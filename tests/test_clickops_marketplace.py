"""AWS Marketplace sellers, from the billing API. Stubbed: no real AWS, ever."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber

from dora_roi.collectors.aws import AwsError
from dora_roi.collectors.clickops import ClickopsError, VendorFact, collect_marketplace


def _ce_response(rows: list[tuple[str, str] | tuple[str, str, str]]) -> dict:
    """Build one `ResultsByTime` page. Each row is `(name, amount)` — defaulting
    to USD — or `(name, amount, unit)` when a test needs a specific or a
    differing currency."""

    def _group(row: tuple[str, str] | tuple[str, str, str]) -> dict:
        name, amount, *rest = row
        unit = rest[0] if rest else "USD"
        return {"Keys": [name], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}

    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2025-08-01", "End": "2026-08-01"},
                "Total": {},
                "Groups": [_group(row) for row in rows],
                "Estimated": False,
            }
        ]
    }


@pytest.fixture
def stubbed():
    client = boto3.client("ce", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    stubber = Stubber(client)
    yield client, stubber
    stubber.deactivate()


class TestMarketplaceSellers:
    def test_a_seller_becomes_a_provider_and_a_fact(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", _ce_response([("Datadog, Inc.", "4200.00")]))
        stubber.activate()

        providers, facts = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))

        assert [p.name for p in providers] == ["datadog"]
        assert providers[0].registry == "aws-marketplace"
        assert providers[0].resource_types["marketplace_subscription"] == 1
        assert facts[0].legal_name == "Datadog, Inc."
        assert facts[0].annual_spend == Decimal("4200.00")
        assert facts[0].source == "aws:ce"
        assert facts[0].currency == "USD"
        assert facts[0].key == "datadog"

    def test_asks_for_exactly_the_marketplace_billing_entity(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            _ce_response([("Datadog, Inc.", "1.00")]),
            {
                "TimePeriod": {"Start": "2025-08-01", "End": "2026-08-01"},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "Filter": {"Dimensions": {"Key": "BILLING_ENTITY", "Values": ["AWS Marketplace"]}},
                "GroupBy": [{"Type": "DIMENSION", "Key": "LEGAL_ENTITY_NAME"}],
            },
        )
        stubber.activate()
        collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))
        stubber.assert_no_pending_responses()

    def test_no_marketplace_spend_is_no_vendors_and_no_refusal(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", _ce_response([]))
        stubber.activate()

        refused: list[str] = []
        providers, facts = collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert (providers, facts, refused) == ([], [], [])

    def test_a_denied_call_is_refused_not_raised(self, stubbed) -> None:
        """An empty result and a denied call are opposite claims about the world."""
        client, stubber = stubbed
        stubber.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException")
        stubber.activate()

        refused: list[str] = []
        providers, facts = collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert (providers, facts) == ([], [])
        assert refused and "ce:GetCostAndUsage" in refused[0]

    def test_a_zero_amount_seller_is_still_a_vendor(self, stubbed) -> None:
        """A free tier is a contractual relationship. Zero is not absent."""
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", _ce_response([("Snyk Limited", "0")]))
        stubber.activate()

        providers, facts = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))

        assert [p.name for p in providers] == ["snyk"]
        assert facts[0].annual_spend == Decimal("0")

    def test_several_sellers_each_get_their_own_row(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage", _ce_response([("Datadog, Inc.", "10.00"), ("Snyk Limited", "20.00")])
        )
        stubber.activate()

        providers, facts = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))

        assert sorted(p.name for p in providers) == ["datadog", "snyk"]
        assert {f.key: f.annual_spend for f in facts} == {"datadog": Decimal("10.00"), "snyk": Decimal("20.00")}

    def test_several_months_of_the_same_seller_accumulate(self, stubbed) -> None:
        client, stubber = stubbed
        payload = _ce_response([("Datadog, Inc.", "10.00")])
        payload["ResultsByTime"].append(dict(payload["ResultsByTime"][0]))
        stubber.add_response("get_cost_and_usage", payload)
        stubber.activate()

        _, facts = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))
        assert facts[0].annual_spend == Decimal("20.00")

    def test_an_unparseable_amount_refuses_rather_than_invents_a_zero(self, stubbed) -> None:
        """Golden rule 1: nothing here upgrades a missing number into a false zero."""
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", _ce_response([("Datadog, Inc.", "not-a-number")]))
        stubber.activate()

        refused: list[str] = []
        providers, facts = collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert (providers, facts) == ([], [])
        assert refused

    def test_the_billing_entity_placeholder_group_itself_is_not_a_vendor(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", _ce_response([("AWS Marketplace", "5.00")]))
        stubber.activate()

        providers, facts = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))
        assert (providers, facts) == ([], [])

    @pytest.mark.parametrize("aws_name", ["Amazon Web Services, Inc.", "AMAZON WEB SERVICES EMEA SARL"])
    def test_aws_named_as_its_own_seller_is_not_a_phantom_vendor(self, stubbed, aws_name: str) -> None:
        """Unverified against a real bill (see the constant's docstring) — but if it
        ever happens, it must not duplicate the `aws` row under a second key."""
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", _ce_response([(aws_name, "5.00")]))
        stubber.activate()

        providers, facts = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))
        assert (providers, facts) == ([], [])

    def test_two_sellers_keep_their_own_currency(self, stubbed) -> None:
        """Regression lock for the per-seller currency fix: with a single global
        `currency` variable this would label one seller with the other's unit."""
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            _ce_response([("Datadog, Inc.", "10.00", "USD"), ("Snyk Limited", "20.00", "EUR")]),
        )
        stubber.activate()

        _, facts = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))

        by_key = {f.key: f.currency for f in facts}
        assert by_key == {"datadog": "USD", "snyk": "EUR"}

    def test_one_sellers_own_lines_mixing_currency_is_refused_not_summed(self, stubbed) -> None:
        """collect_annual_expense treats this as fatal for the same reason: adding
        the two would invent an exchange rate, and this is the authoritative
        channel — it must not be laxer about it than the inferred one."""
        client, stubber = stubbed
        payload = _ce_response([("Datadog, Inc.", "10.00", "USD")])
        payload["ResultsByTime"].append(_ce_response([("Datadog, Inc.", "5.00", "EUR")])["ResultsByTime"][0])
        stubber.add_response("get_cost_and_usage", payload)
        stubber.activate()

        refused: list[str] = []
        providers, facts = collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert (providers, facts) == ([], [])
        assert refused and "currenc" in refused[0].lower()

    def test_a_truncated_page_is_followed_not_dropped(self, stubbed) -> None:
        """A real seller sitting on page 2 must not vanish as though it never
        billed anything — that is indistinguishable from it not existing."""
        client, stubber = stubbed
        page1 = _ce_response([("Datadog, Inc.", "10.00")])
        page1["NextPageToken"] = "page-2"
        page2 = _ce_response([("Snyk Limited", "5.00")])
        stubber.add_response("get_cost_and_usage", page1)
        stubber.add_response(
            "get_cost_and_usage",
            page2,
            {
                "TimePeriod": {"Start": "2025-08-01", "End": "2026-08-01"},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "Filter": {"Dimensions": {"Key": "BILLING_ENTITY", "Values": ["AWS Marketplace"]}},
                "GroupBy": [{"Type": "DIMENSION", "Key": "LEGAL_ENTITY_NAME"}],
                "NextPageToken": "page-2",
            },
        )
        stubber.activate()

        providers, facts = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))

        assert sorted(p.name for p in providers) == ["datadog", "snyk"]
        assert {f.key for f in facts} == {"datadog", "snyk"}
        stubber.assert_no_pending_responses()


class TestTwoSellerNamesUnderOneKey:
    """C1: `_totals_by_seller` groups by the raw `LEGAL_ENTITY_NAME` and
    `vendor_key` then folds the legal form away, so `'Datadog, Inc.'` and
    `'Datadog International Ltd'` arrive as two totals and leave as one key.
    Emitting a fact per name meant `cli._apply_vendor_facts`'s `{fact.key:
    fact}` kept whichever came last and dropped the other's spend without a
    word — a FILLED annual expense that is a fraction of the bill.
    """

    def test_neither_colliding_seller_becomes_a_fact(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            _ce_response([("Datadog, Inc.", "4200.00"), ("Datadog International Ltd", "99.00")]),
        )
        stubber.activate()

        refused: list[str] = []
        providers, facts = collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert facts == []
        assert [p.name for p in providers] == ["datadog"]
        assert len(refused) == 1

    def test_the_refusal_names_both_sellers_and_both_amounts(self, stubbed) -> None:
        """The two numbers are the whole point: a reader has to be able to see
        that 4200.00 was never the whole of it."""
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            _ce_response([("Datadog, Inc.", "4200.00"), ("Datadog International Ltd", "99.00")]),
        )
        stubber.activate()

        refused: list[str] = []
        collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        message = refused[0]
        assert "'Datadog, Inc.' (4200.00 USD)" in message
        assert "'Datadog International Ltd' (99.00 USD)" in message
        assert "'datadog'" in message

    def test_one_ambiguous_key_does_not_cost_the_other_sellers_their_facts(self, stubbed) -> None:
        """Narrower than the mixed-currency precedent, which refuses the whole
        response: this ambiguity is confined to one key, and Snyk's own fact is
        not in doubt because Datadog's is."""
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            _ce_response(
                [
                    ("Datadog, Inc.", "4200.00"),
                    ("Datadog International Ltd", "99.00"),
                    ("Snyk Limited", "20.00"),
                ]
            ),
        )
        stubber.activate()

        refused: list[str] = []
        providers, facts = collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert [(f.key, f.annual_spend) for f in facts] == [("snyk", Decimal("20.00"))]
        assert sorted(p.name for p in providers) == ["datadog", "snyk"]

    def test_the_ambiguous_vendor_is_still_reported_as_a_provider(self, stubbed) -> None:
        """A counterparty seen on the bill must not disappear from the register
        because the tool cannot say which of two names it files under."""
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            _ce_response([("Confluent Inc", "10.00"), ("Confluent International Ltd", "20.00")]),
        )
        stubber.activate()

        providers, _ = collect_marketplace(client=client, refused=[], today=date(2026, 8, 22))

        assert len(providers) == 1
        assert providers[0].name == "confluent"
        assert providers[0].resource_types["marketplace_subscription"] == 2
        assert providers[0].resource_count == 2

    def test_one_seller_respelled_across_the_window_is_the_same_collision(self, stubbed) -> None:
        """It does not take two legal entities. `Datadog Inc` in March and
        `Datadog, Inc.` in April are two `LEGAL_ENTITY_NAME` groups and one
        key."""
        client, stubber = stubbed
        payload = _ce_response([("Datadog Inc", "10.00")])
        payload["ResultsByTime"].append(_ce_response([("Datadog, Inc.", "5.00")])["ResultsByTime"][0])
        stubber.add_response("get_cost_and_usage", payload)
        stubber.activate()

        refused: list[str] = []
        _, facts = collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert facts == []
        assert refused and "ambiguous seller" in refused[0]

    def test_the_same_name_billed_twice_is_not_a_collision(self, stubbed) -> None:
        """The negative case for the test above: one name across two months is
        one seller, and must still produce its fact with both months summed."""
        client, stubber = stubbed
        payload = _ce_response([("Datadog, Inc.", "10.00")])
        payload["ResultsByTime"].append(_ce_response([("Datadog, Inc.", "5.00")])["ResultsByTime"][0])
        stubber.add_response("get_cost_and_usage", payload)
        stubber.activate()

        refused: list[str] = []
        _, facts = collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert [(f.legal_name, f.annual_spend) for f in facts] == [("Datadog, Inc.", Decimal("15.00"))]
        assert refused == []

    def test_the_refusal_is_classified_as_answered_not_as_a_failed_call(self, stubbed) -> None:
        """Cross-module lock: `methodology.refusal_kind` spells this prefix out
        for itself, so the two copies have to be checked against each other or
        the console files this under "Unavailable" — a claim that the billing
        call could not be made, next to the facts it did produce."""
        from dora_roi.report.methodology import refusal_kind

        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            _ce_response([("Datadog, Inc.", "1.00"), ("Datadog International Ltd", "2.00")]),
        )
        stubber.activate()

        refused: list[str] = []
        collect_marketplace(client=client, refused=refused, today=date(2026, 8, 22))

        assert refusal_kind(refused[0]) == "ambiguous"
        assert refusal_kind("marketplace: ce:GetCostAndUsage unavailable (X: y)") == "unavailable"


class TestMarketplaceCredentials:
    def test_a_bad_profile_degrades_the_channel_instead_of_killing_the_scan(self) -> None:
        """Client construction must sit inside the same no-raise guard as the AWS
        call: a typo in sources.yaml's `aws:` profile is not a reason to hand
        back no register at all. No Stubber here on purpose — boto3 raises
        ProfileNotFound locally, before any network call is attempted, so this
        exercises the real `_ce_client` path without touching the network."""
        refused: list[str] = []
        providers, facts = collect_marketplace(
            profile="dora-roi-test-profile-that-does-not-exist", refused=refused, today=date(2026, 8, 22)
        )

        assert (providers, facts) == ([], [])
        assert refused
        assert "marketplace" in refused[0].lower()


class TestVendorFact:
    def test_is_a_frozen_dataclass_with_the_documented_fields(self) -> None:
        fact = VendorFact(
            key="datadog", legal_name="Datadog, Inc.", annual_spend=Decimal("1"), currency="USD", source="aws:ce"
        )
        with pytest.raises(AttributeError):
            fact.key = "other"  # type: ignore[misc]


class TestClickopsError:
    def test_is_its_own_exception_type(self) -> None:
        assert issubclass(ClickopsError, Exception)
        assert not issubclass(AwsError, ClickopsError)
        assert not issubclass(ClickopsError, AwsError)
