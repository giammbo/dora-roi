"""GLEIF API v1 client. No network: every call goes through MockTransport."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from dora_roi.enrichment.gleif import GleifClient, GleifError, LeiRecord, MatchType

AWS_LEI = "5493001KJTIIGC8Y1R12"
AWS_NAME = "AMAZON WEB SERVICES EMEA SARL"


def lei_record_payload(lei: str = AWS_LEI, name: str = AWS_NAME, country: str = "LU", status: str = "ACTIVE") -> dict:
    return {
        "type": "lei-records",
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": {
                "legalName": {"name": name, "language": "en"},
                "legalAddress": {"country": country, "city": "Luxembourg"},
                "status": status,
            },
            "registration": {"status": "ISSUED"},
        },
    }


def fuzzy_payload(lei: str = AWS_LEI, value: str = AWS_NAME) -> dict:
    return {
        "data": [
            {
                "type": "fuzzycompletions",
                "attributes": {"value": value},
                "relationships": {"lei-records": {"data": {"type": "lei-records", "id": lei}}},
            }
        ]
    }


class Recorder:
    """A MockTransport handler that records what was asked and replies from a script."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlparse(str(request.url)).path
        reply = self.routes.get(path, httpx.Response(404, json={"errors": [{"status": "404"}]}))
        return reply() if callable(reply) else reply

    @property
    def paths(self) -> list[str]:
        return [urlparse(str(r.url)).path for r in self.requests]

    def query(self, index: int = 0) -> dict[str, list[str]]:
        return parse_qs(urlparse(str(self.requests[index].url)).query)


def client(recorder: Recorder, **kw: Any) -> GleifClient:
    defaults: dict[str, Any] = {"min_interval": 0.0, "sleep": lambda _s: None, "clock": lambda: 0.0}
    return GleifClient(transport=httpx.MockTransport(recorder), **{**defaults, **kw})


class TestLookupByLei:
    def test_parses_the_record(self) -> None:
        rec = Recorder({f"/api/v1/lei-records/{AWS_LEI}": httpx.Response(200, json={"data": lei_record_payload()})})
        with client(rec) as gleif:
            record = gleif.lookup_by_lei(AWS_LEI)
        assert record == LeiRecord(
            lei=AWS_LEI, legal_name=AWS_NAME, country="LU", status="ACTIVE", match_type=MatchType.EXACT
        )

    def test_404_is_none_not_an_error(self) -> None:
        rec = Recorder({})
        with client(rec) as gleif:
            assert gleif.lookup_by_lei("00000000000000000000") is None

    def test_sends_the_json_api_accept_header(self) -> None:
        rec = Recorder({f"/api/v1/lei-records/{AWS_LEI}": httpx.Response(200, json={"data": lei_record_payload()})})
        with client(rec) as gleif:
            gleif.lookup_by_lei(AWS_LEI)
        assert rec.requests[0].headers["accept"] == "application/vnd.api+json"


class TestSearchByName:
    def test_exact_search_parses_lei_name_country_and_status(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": [lei_record_payload()]})})
        with client(rec) as gleif:
            results = gleif.search_by_name("Amazon Web Services EMEA SARL")
        assert len(results) == 1
        assert (results[0].lei, results[0].legal_name, results[0].country, results[0].status) == (
            AWS_LEI,
            AWS_NAME,
            "LU",
            "ACTIVE",
        )
        assert results[0].match_type is MatchType.EXACT

    def test_filters_on_the_legal_name(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": []})})
        with client(rec) as gleif:
            gleif.search_by_name("Acme SpA")
        assert rec.query()["filter[entity.legalName]"] == ["Acme SpA"]

    def test_no_hit_is_an_empty_list(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": []})})
        with client(rec) as gleif:
            assert gleif.search_by_name("Nothing Ltd") == []


class TestFuzzySearch:
    def test_resolves_the_lei_from_relationships(self) -> None:
        rec = Recorder({"/api/v1/fuzzycompletions": httpx.Response(200, json=fuzzy_payload())})
        with client(rec) as gleif:
            results = gleif.fuzzy_search("amazon web services")
        assert results[0].lei == AWS_LEI
        assert results[0].legal_name == AWS_NAME
        assert results[0].match_type is MatchType.FUZZY

    def test_a_candidate_without_a_lei_is_dropped(self) -> None:
        payload = {"data": [{"type": "fuzzycompletions", "attributes": {"value": "Something"}, "relationships": {}}]}
        rec = Recorder({"/api/v1/fuzzycompletions": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            assert gleif.fuzzy_search("something") == []

    def test_country_and_status_are_unknown_until_resolved(self) -> None:
        rec = Recorder({"/api/v1/fuzzycompletions": httpx.Response(200, json=fuzzy_payload())})
        with client(rec) as gleif:
            results = gleif.fuzzy_search("amazon")
        assert results[0].country is None
        assert results[0].status is None


class TestBestMatch:
    def test_exact_wins_and_never_calls_fuzzy(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": [lei_record_payload()]})})
        with client(rec) as gleif:
            match = gleif.best_match("Amazon Web Services EMEA SARL")
        assert match is not None and match.match_type is MatchType.EXACT
        assert "/api/v1/fuzzycompletions" not in rec.paths

    def test_falls_back_to_fuzzy_and_says_so(self) -> None:
        rec = Recorder(
            {
                "/api/v1/lei-records": httpx.Response(200, json={"data": []}),
                "/api/v1/fuzzycompletions": httpx.Response(200, json=fuzzy_payload()),
                f"/api/v1/lei-records/{AWS_LEI}": httpx.Response(200, json={"data": lei_record_payload()}),
            }
        )
        with client(rec) as gleif:
            match = gleif.best_match("amazon web services")
        assert match is not None
        assert match.match_type is MatchType.FUZZY
        # Resolved to the full record, so country and status are usable.
        assert match.country == "LU"
        assert match.status == "ACTIVE"

    def test_nothing_found_is_none(self) -> None:
        rec = Recorder(
            {
                "/api/v1/lei-records": httpx.Response(200, json={"data": []}),
                "/api/v1/fuzzycompletions": httpx.Response(200, json={"data": []}),
            }
        )
        with client(rec) as gleif:
            assert gleif.best_match("Nothing Ltd") is None

    def test_the_caller_can_tell_exact_from_fuzzy(self) -> None:
        """The FILLED / INFERRED decision is the caller's, made on match_type."""
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": [lei_record_payload()]})})
        with client(rec) as gleif:
            match = gleif.best_match("Amazon Web Services EMEA SARL")
        assert match is not None
        assert match.match_type in set(MatchType)


class TestNameCache:
    def test_the_same_name_is_only_asked_once(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": [lei_record_payload()]})})
        with client(rec) as gleif:
            gleif.best_match("Amazon Web Services EMEA SARL")
            gleif.best_match("Amazon Web Services EMEA SARL")
        assert len(rec.requests) == 1

    def test_a_miss_is_cached_too(self) -> None:
        rec = Recorder(
            {
                "/api/v1/lei-records": httpx.Response(200, json={"data": []}),
                "/api/v1/fuzzycompletions": httpx.Response(200, json={"data": []}),
            }
        )
        with client(rec) as gleif:
            gleif.best_match("Nothing Ltd")
            gleif.best_match("Nothing Ltd")
        assert len(rec.requests) == 2  # one exact + one fuzzy, not four

    def test_a_lei_lookup_is_cached(self) -> None:
        rec = Recorder({f"/api/v1/lei-records/{AWS_LEI}": httpx.Response(200, json={"data": lei_record_payload()})})
        with client(rec) as gleif:
            gleif.lookup_by_lei(AWS_LEI)
            gleif.lookup_by_lei(AWS_LEI)
        assert len(rec.requests) == 1


class TestThrottle:
    def test_waits_between_calls(self) -> None:
        slept: list[float] = []
        now = [0.0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": []})})
        with client(rec, min_interval=1.0, clock=clock, sleep=sleep) as gleif:
            gleif.search_by_name("one")
            gleif.search_by_name("two")
        assert slept == [pytest.approx(1.0)]

    def test_does_not_wait_when_enough_time_already_passed(self) -> None:
        slept: list[float] = []
        ticks = iter([0.0, 5.0, 5.0, 10.0])
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": []})})
        with client(rec, min_interval=1.0, clock=lambda: next(ticks), sleep=slept.append) as gleif:
            gleif.search_by_name("one")
            gleif.search_by_name("two")
        assert slept == []

    def test_the_default_interval_is_at_least_one_second(self) -> None:
        rec = Recorder({})
        with GleifClient(transport=httpx.MockTransport(rec)) as gleif:
            assert gleif.min_interval >= 1.0


class TestErrors:
    def test_a_transport_failure_raises_gleif_error(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host", request=request)

        with GleifClient(transport=httpx.MockTransport(boom), min_interval=0.0) as gleif:
            with pytest.raises(GleifError, match="GLEIF"):
                gleif.search_by_name("Acme")

    def test_a_server_error_raises_gleif_error(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(503, text="upstream down")})
        with client(rec) as gleif:
            with pytest.raises(GleifError, match="503"):
                gleif.search_by_name("Acme")

    def test_an_unparseable_body_raises_gleif_error(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, text="<html>nope</html>")})
        with client(rec) as gleif:
            with pytest.raises(GleifError):
                gleif.search_by_name("Acme")

    def test_a_record_missing_its_lei_is_skipped_not_fatal(self) -> None:
        payload = {"data": [{"type": "lei-records", "id": "", "attributes": {"entity": {}}}]}
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            assert gleif.search_by_name("Acme") == []


class TestContextManager:
    def test_closing_twice_is_harmless(self) -> None:
        rec = Recorder({})
        gleif = client(rec)
        gleif.close()
        gleif.close()

    def test_usable_without_the_with_statement(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": []})})
        gleif = client(rec)
        try:
            assert gleif.search_by_name("Acme") == []
        finally:
            gleif.close()


def test_json_api_shape_is_what_the_docs_describe() -> None:
    """Guard against a fixture drifting from the payload GLEIF actually returns."""
    payload = json.dumps({"data": lei_record_payload()})
    assert '"legalName"' in payload and '"legalAddress"' in payload


class TestExactMeansTheNameCameBackTheSame:
    """GLEIF's legalName filter is a search, not an equality test."""

    def test_a_different_name_is_a_candidate_not_a_fact(self) -> None:
        """The real case: "Red Hat, Inc." answers with "Red Hat AB" in Sweden."""
        payload = {"data": [lei_record_payload(lei="5493000000000000AB01", name="Red Hat AB", country="SE")]}
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            match = gleif.best_match("Red Hat, Inc.")
        assert match is not None
        assert match.match_type is MatchType.FUZZY

    def test_the_same_name_is_exact(self) -> None:
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json={"data": [lei_record_payload()]})})
        with client(rec) as gleif:
            match = gleif.best_match(AWS_NAME)
        assert match is not None and match.match_type is MatchType.EXACT

    def test_case_and_legal_punctuation_are_noise(self) -> None:
        payload = {"data": [lei_record_payload(name="AMAZON WEB SERVICES, INC.")]}
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            match = gleif.best_match("Amazon Web Services, Inc.")
        assert match is not None and match.match_type is MatchType.EXACT

    def test_a_different_legal_form_is_not_the_same_entity(self) -> None:
        """Normalising legal forms away would make Red Hat, Inc. match Red Hat AB."""
        payload = {"data": [lei_record_payload(name="Acme AB")]}
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            match = gleif.best_match("Acme, Inc.")
        assert match is not None and match.match_type is MatchType.FUZZY

    def test_the_right_entity_is_picked_out_of_a_page_of_near_misses(self) -> None:
        payload = {
            "data": [
                lei_record_payload(lei="5493000000000000XX01", name="Amazon Web Services Chile"),
                lei_record_payload(),
            ]
        }
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            match = gleif.best_match(AWS_NAME)
        assert match is not None
        assert match.lei == AWS_LEI
        assert match.match_type is MatchType.EXACT


class TestACandidateMustShareARealWord:
    """Sharing only a legal form is not sharing a name."""

    def test_the_inc_group_case(self) -> None:
        """GLEIF answers "Auth0, Inc." with "INC Group Inc." — matching on "Inc"."""
        payload = {"data": [lei_record_payload(lei="5493001AHR8KMMFIS520", name="INC Group Inc.", country="CA")]}
        rec = Recorder(
            {
                "/api/v1/lei-records": httpx.Response(200, json=payload),
                "/api/v1/fuzzycompletions": httpx.Response(200, json={"data": []}),
            }
        )
        with client(rec) as gleif:
            assert gleif.best_match("Auth0, Inc.") is None

    def test_three_vendors_do_not_collapse_onto_one_lei(self) -> None:
        payload = {"data": [lei_record_payload(lei="5493001AHR8KMMFIS520", name="INC Group Inc.", country="CA")]}
        rec = Recorder(
            {
                "/api/v1/lei-records": httpx.Response(200, json=payload),
                "/api/v1/fuzzycompletions": httpx.Response(200, json={"data": []}),
            }
        )
        with client(rec) as gleif:
            got = [gleif.best_match(n) for n in ("Auth0, Inc.", "Netlify, Inc.", "Webflow, Inc.")]
        assert got == [None, None, None]

    def test_a_real_word_in_common_still_qualifies(self) -> None:
        payload = {"data": [lei_record_payload(lei="5493001AHR8KMMFIS520", name="RAINTANK INC.", country="US")]}
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            match = gleif.best_match("Raintank, Inc.")
        assert match is not None and match.match_type is MatchType.EXACT

    def test_a_different_legal_form_on_a_shared_name_is_still_a_candidate(self) -> None:
        """Red Hat AB is the wrong Red Hat, but it is not a random company."""
        payload = {"data": [lei_record_payload(lei="5493001AHR8KMMFIS520", name="Red Hat AB", country="SE")]}
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            match = gleif.best_match("Red Hat, Inc.")
        assert match is not None and match.match_type is MatchType.FUZZY

    def test_the_right_one_wins_over_a_merely_plausible_one(self) -> None:
        payload = {
            "data": [
                lei_record_payload(lei="5493001AHR8KMMFIS520", name="Amazon Web Services Chile"),
                lei_record_payload(),
            ]
        }
        rec = Recorder({"/api/v1/lei-records": httpx.Response(200, json=payload)})
        with client(rec) as gleif:
            match = gleif.best_match(AWS_NAME)
        assert match is not None and match.lei == AWS_LEI and match.match_type is MatchType.EXACT
