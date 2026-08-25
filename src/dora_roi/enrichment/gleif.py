"""GLEIF API v1 client: legal name to LEI, HQ country and registration status.

The register lives or dies on LEIs — roughly a third of the 2025 first-filing
failures were missing or invalid ones — so this is the one
enrichment step that can legitimately produce a ``FILLED`` value.

Which is exactly why the client does not decide that itself. Every result
carries a :class:`MatchType`, and the caller reads it: an exact name match is
strong enough to be FILLED, a fuzzy one is a candidate and must stay INFERRED
with the alternative recorded. Folding that decision in here would let a fuzzy
guess reach a filing wearing the same badge as a confirmed identity.

Two operational rules, both about being a good citizen on a free
API with no key: at least one second between calls, and one question per name
per run.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

import httpx

__all__ = ["GleifClient", "GleifError", "LeiRecord", "MatchType"]

GLEIF_BASE_URL = "https://api.gleif.org/api/v1"
JSON_API_ACCEPT = "application/vnd.api+json"
MIN_INTERVAL_SECONDS = 1.0

#: Punctuation that separates a name from its legal form without changing it.
_NOISE = re.compile(r"[.,\-\u2019']")

#: Legal forms. Stripped before deciding whether two names have anything in
#: common, because they are the one token every company shares. Left in, GLEIF's
#: answer to "Auth0, Inc." is "INC Group Inc." — and to "Netlify, Inc." and
#: "Webflow, Inc." it is the *same* Canadian company, which is how one register
#: ended up giving three vendors one LEI.
_LEGAL_FORMS = frozenset(
    {
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "llc",
        "lp",
        "llp",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "ag",
        "sa",
        "sas",
        "sarl",
        "srl",
        "spa",
        "bv",
        "nv",
        "ab",
        "as",
        "oy",
        "aps",
        "kft",
        "sp",
        "zoo",
        "pty",
        "group",
        "holding",
        "holdings",
        "international",
    }
)


class GleifError(Exception):
    """GLEIF could not be reached, or answered with something unusable."""


class MatchType(StrEnum):
    """How a record was found. The caller turns this into FILLED or INFERRED."""

    EXACT = "exact"
    FUZZY = "fuzzy"


@dataclass(frozen=True)
class LeiRecord:
    """One GLEIF record, reduced to what the register actually needs."""

    lei: str
    legal_name: str
    country: str | None
    status: str | None
    match_type: MatchType

    @property
    def is_active(self) -> bool:
        """Registration status ACTIVE. Pre-flight refuses anything else."""
        return self.status == "ACTIVE"


class GleifClient:
    """Throttled, cached, context-managed client for GLEIF API v1.

    ``clock`` and ``sleep`` are injectable so the throttle can be tested without
    a test suite that actually waits.
    """

    def __init__(
        self,
        *,
        base_url: str = GLEIF_BASE_URL,
        min_interval: float = MIN_INTERVAL_SECONDS,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_call: float | None = None
        self._name_cache: dict[str, LeiRecord | None] = {}
        self._lei_cache: dict[str, LeiRecord | None] = {}
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": JSON_API_ACCEPT},
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- public API ---------------------------------------------------------

    def lookup_by_lei(self, lei: str) -> LeiRecord | None:
        """Fetch a known LEI. ``None`` when GLEIF does not have it (404).

        Used to pre-validate an LEI a user asserted in the overlay: a code that
        is well-formed but unknown to GLEIF is exactly the failure mode that
        gets a filing rejected.
        """
        if lei in self._lei_cache:
            return self._lei_cache[lei]
        payload = self._get(f"/lei-records/{lei}", allow_404=True)
        record = None
        if payload is not None:
            data = payload.get("data")
            record = _parse_record(data, MatchType.EXACT) if isinstance(data, dict) else None
        self._lei_cache[lei] = record
        return record

    def search_by_name(self, name: str, page_size: int = 5) -> list[LeiRecord]:
        """Exact-ish search on the legal name."""
        payload = self._get(
            "/lei-records",
            params={"filter[entity.legalName]": name, "page[size]": page_size},
        )
        return _parse_records(payload, MatchType.EXACT)

    def fuzzy_search(self, name: str) -> list[LeiRecord]:
        """Fuzzy candidates. The LEI comes from ``relationships.lei-records``.

        Candidates carry no country or status — those need the full record, and
        :meth:`best_match` fetches it when a fuzzy hit is the one it keeps.
        """
        payload = self._get("/fuzzycompletions", params={"field": "entity.legalName", "q": name})
        candidates = []
        for item in _data_list(payload):
            lei = _fuzzy_lei(item)
            value = (item.get("attributes") or {}).get("value")
            if lei and isinstance(value, str):
                candidates.append(
                    LeiRecord(lei=lei, legal_name=value, country=None, status=None, match_type=MatchType.FUZZY)
                )
        return candidates

    def best_match(self, name: str) -> LeiRecord | None:
        """Exact first, fuzzy second, cached by name for the run.

        "Exact" is decided here, on the name that came back — never on which
        endpoint answered. GLEIF's ``filter[entity.legalName]`` is a search, not
        an equality test: it answers "Red Hat, Inc." with "Red Hat AB" in Sweden
        and "Auth0, Inc." with "INC Group Inc." in Canada. Trusting the endpoint
        would mark both FILLED, and a register would name the wrong counterparty
        with the tool asserting it as authoritative. A result whose name does not
        match the query is a candidate, and candidates are INFERRED.

        A kept fuzzy candidate is resolved to its full record, so the caller
        gets a usable country and status either way and only has to branch on
        ``match_type`` for the provenance decision. That second call costs a
        throttle interval, which is the right trade: a provider identified
        without a country is a gap the report will raise anyway.
        """
        if name in self._name_cache:
            return self._name_cache[name]

        match: LeiRecord | None = None
        found = self.search_by_name(name, page_size=5)
        confirmed = next((r for r in found if _same_name(r.legal_name, name)), None)
        plausible = next((r for r in found if _shares_a_word(r.legal_name, name)), None)

        if confirmed is not None:
            match = confirmed
        elif plausible is not None:
            # The search answered with somebody adjacent. Keep it, downgraded.
            match = LeiRecord(
                lei=plausible.lei,
                legal_name=plausible.legal_name,
                country=plausible.country,
                status=plausible.status,
                match_type=MatchType.FUZZY,
            )
        else:
            # Either the search found nothing, or everything it found shared only
            # a legal form with the query. Ask the endpoint built for guessing.
            candidates = [c for c in self.fuzzy_search(name) if _shares_a_word(c.legal_name, name)]
            if candidates:
                resolved = self.lookup_by_lei(candidates[0].lei)
                match = (
                    LeiRecord(
                        lei=resolved.lei,
                        legal_name=resolved.legal_name,
                        country=resolved.country,
                        status=resolved.status,
                        match_type=MatchType.FUZZY,
                    )
                    if resolved is not None
                    else candidates[0]
                )

        self._name_cache[name] = match
        return match

    # -- transport ----------------------------------------------------------

    def _get(
        self, path: str, params: dict[str, Any] | None = None, *, allow_404: bool = False
    ) -> dict[str, Any] | None:
        self._throttle()
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise GleifError(f"GLEIF request to {path} failed: {e}") from e

        if response.status_code == httpx.codes.NOT_FOUND and allow_404:
            return None
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise GleifError(f"GLEIF answered {response.status_code} for {path}")

        try:
            payload = response.json()
        except ValueError as e:
            raise GleifError(f"GLEIF answered {path} with a body that is not JSON: {e}") from e
        if not isinstance(payload, dict):
            raise GleifError(f"GLEIF answered {path} with {type(payload).__name__}, expected a JSON:API object")
        return payload

    def _throttle(self) -> None:
        if self._last_call is not None:
            waited = self._clock() - self._last_call
            if waited < self.min_interval:
                self._sleep(self.min_interval - waited)
        self._last_call = self._clock()


def _same_name(found: str, queried: str) -> bool:
    """Whether GLEIF returned the entity that was asked for.

    Deliberately shy of clever. Case and the punctuation around a legal form are
    noise — "AMAZON WEB SERVICES, INC." is the same entity as "Amazon Web
    Services, Inc." — but nothing else is normalised away. Stripping legal forms
    too would make "Red Hat, Inc." match "Red Hat AB", which is the exact
    mistake this function exists to prevent.
    """
    return _normalise(found) == _normalise(queried)


def _shares_a_word(found: str, queried: str) -> bool:
    """Whether two names have anything in common beyond being companies."""
    return bool(_significant(found) & _significant(queried))


def _significant(name: str) -> set[str]:
    return {word for word in _normalise(name).split() if word not in _LEGAL_FORMS}


def _normalise(name: str) -> str:
    return " ".join(_NOISE.sub(" ", name).casefold().split())


def _data_list(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = (payload or {}).get("data")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _parse_records(payload: dict[str, Any] | None, match_type: MatchType) -> list[LeiRecord]:
    parsed = (_parse_record(item, match_type) for item in _data_list(payload))
    return [record for record in parsed if record is not None]


def _parse_record(item: dict[str, Any], match_type: MatchType) -> LeiRecord | None:
    """Map one JSON:API lei-record. Returns None rather than raising on a broken one.

    A single malformed record in a page is GLEIF's problem, not a reason to
    abandon a scan that is best-effort by design.
    """
    attributes = item.get("attributes") or {}
    entity = attributes.get("entity") or {}
    lei = attributes.get("lei") or item.get("id")
    if not isinstance(lei, str) or not lei:
        return None

    legal_name = (entity.get("legalName") or {}).get("name")
    country = (entity.get("legalAddress") or {}).get("country")
    status = entity.get("status")
    return LeiRecord(
        lei=lei,
        legal_name=legal_name if isinstance(legal_name, str) else "",
        country=country if isinstance(country, str) else None,
        status=status if isinstance(status, str) else None,
        match_type=match_type,
    )


def _fuzzy_lei(item: dict[str, Any]) -> str | None:
    """Dig the LEI out of a fuzzycompletions relationship."""
    data = ((item.get("relationships") or {}).get("lei-records") or {}).get("data")
    if isinstance(data, dict):
        lei = data.get("id")
        return lei if isinstance(lei, str) and lei else None
    return None
