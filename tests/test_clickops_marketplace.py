"""AWS Marketplace sellers, from the billing API. Stubbed: no real AWS, ever."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber

from dora_roi.collectors.aws import AwsError
from dora_roi.collectors.clickops import ClickopsError, VendorFact, collect_marketplace


def _ce_response(rows: list[tuple[str, str]], unit: str = "USD") -> dict:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2025-08-01", "End": "2026-08-01"},
                "Total": {},
                "Groups": [
                    {"Keys": [name], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}
                    for name, amount in rows
                ],
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
