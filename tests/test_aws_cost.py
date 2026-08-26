"""Cost Explorer annual expense. Stubbed responses: no real AWS, ever."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber

from dora_roi.collectors.aws import (
    AwsError,
    ExpenseReport,
    collect_annual_expense,
    expense_for_provider,
)


def ce_response(rows: dict[str, str], unit: str = "EUR") -> dict:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2025-08-01", "End": "2025-09-01"},
                "Total": {},
                "Groups": [
                    {"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}
                    for service, amount in rows.items()
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


class TestCollectAnnualExpense:
    def test_sums_the_trailing_twelve_months_per_service(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            ce_response({"Amazon Elastic Compute Cloud - Compute": "1200.50", "Amazon S3": "300.25"}),
        )
        stubber.activate()
        report = collect_annual_expense(client=client, today=date(2026, 8, 22))
        assert report.by_service["Amazon S3"] == Decimal("300.25")
        assert report.total == Decimal("1500.75")

    def test_a_second_page_of_services_is_not_dropped(self, stubbed) -> None:
        """C4: this call groups twelve months by SERVICE, so it produces more
        groups than the Marketplace call that already paginated — and its
        figure is what `cli` marks FILLED for the `aws` row. A truncated page
        makes a service's spend vanish exactly as though it were never
        billed."""
        client, stubber = stubbed
        page1 = ce_response({"Amazon S3": "300.25"})
        page1["NextPageToken"] = "page-2"
        stubber.add_response("get_cost_and_usage", page1)
        stubber.add_response(
            "get_cost_and_usage",
            ce_response({"Amazon Relational Database Service": "900.00"}),
            {
                "TimePeriod": {"Start": "2025-08-01", "End": "2026-08-01"},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
                "NextPageToken": "page-2",
            },
        )
        stubber.activate()

        report = collect_annual_expense(client=client, today=date(2026, 8, 22))

        assert report.by_service["Amazon Relational Database Service"] == Decimal("900.00")
        assert report.total == Decimal("1200.25")
        stubber.assert_no_pending_responses()

    def test_a_single_page_makes_exactly_one_call(self, stubbed) -> None:
        """The negative half: no token means no second request, so the loop
        cannot turn one bill into two."""
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", ce_response({"Amazon S3": "300.25"}))
        stubber.activate()

        report = collect_annual_expense(client=client, today=date(2026, 8, 22))

        assert report.total == Decimal("300.25")
        stubber.assert_no_pending_responses()

    def test_asks_for_exactly_twelve_trailing_months(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response(
            "get_cost_and_usage",
            ce_response({"Amazon S3": "1.00"}),
            {
                "TimePeriod": {"Start": "2025-08-01", "End": "2026-08-01"},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            },
        )
        stubber.activate()
        collect_annual_expense(client=client, today=date(2026, 8, 22))
        stubber.assert_no_pending_responses()

    def test_currency_comes_from_the_response(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", ce_response({"Amazon S3": "1.00"}, unit="USD"))
        stubber.activate()
        assert collect_annual_expense(client=client, today=date(2026, 8, 22)).currency == "USD"

    def test_several_months_accumulate(self, stubbed) -> None:
        client, stubber = stubbed
        payload = ce_response({"Amazon S3": "10.00"})
        payload["ResultsByTime"].append(dict(payload["ResultsByTime"][0]))
        stubber.add_response("get_cost_and_usage", payload)
        stubber.activate()
        assert collect_annual_expense(client=client, today=date(2026, 8, 22)).total == Decimal("20.00")

    def test_money_is_decimal_not_float(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", ce_response({"Amazon S3": "0.10", "Amazon EC2": "0.20"}))
        stubber.activate()
        report = collect_annual_expense(client=client, today=date(2026, 8, 22))
        assert report.total == Decimal("0.30")
        assert isinstance(report.total, Decimal)

    def test_mixed_currencies_are_refused_rather_than_added_up(self, stubbed) -> None:
        client, stubber = stubbed
        payload = ce_response({"Amazon S3": "1.00"})
        payload["ResultsByTime"][0]["Groups"].append(
            {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "2.00", "Unit": "USD"}}}
        )
        stubber.add_response("get_cost_and_usage", payload)
        stubber.activate()
        with pytest.raises(AwsError, match="currenc"):
            collect_annual_expense(client=client, today=date(2026, 8, 22))

    def test_an_empty_bill_is_not_an_error(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", {"ResultsByTime": []})
        stubber.activate()
        report = collect_annual_expense(client=client, today=date(2026, 8, 22))
        assert report.total == Decimal("0")
        assert report.currency is None


class TestProviderToExpense:
    def test_aws_gets_the_whole_bill(self) -> None:
        report = ExpenseReport(
            currency="EUR",
            by_service={"Amazon S3": Decimal("10"), "Amazon EC2": Decimal("20")},
            period=(date(2025, 8, 1), date(2026, 8, 1)),
        )
        assert expense_for_provider("aws", report) == Decimal("30")

    def test_a_marketplace_vendor_gets_only_its_own_lines(self) -> None:
        report = ExpenseReport(
            currency="EUR",
            by_service={
                "Amazon S3": Decimal("10"),
                "Datadog, Inc. - Datadog Pro": Decimal("7"),
                "Datadog, Inc. - APM": Decimal("3"),
            },
            period=(date(2025, 8, 1), date(2026, 8, 1)),
        )
        assert expense_for_provider("datadog", report) == Decimal("10")

    def test_a_vendor_with_no_aws_line_gets_nothing_not_zero(self) -> None:
        """None means "AWS cannot price this"; zero would mean "it is free"."""
        report = ExpenseReport(
            currency="EUR", by_service={"Amazon S3": Decimal("10")}, period=(date(2025, 8, 1), date(2026, 8, 1))
        )
        assert expense_for_provider("cloudflare", report) is None

    def test_an_unknown_provider_gets_nothing(self) -> None:
        report = ExpenseReport(
            currency="EUR", by_service={"Amazon S3": Decimal("10")}, period=(date(2025, 8, 1), date(2026, 8, 1))
        )
        assert expense_for_provider("weirdvendor", report) is None

    def test_matching_ignores_case(self) -> None:
        report = ExpenseReport(
            currency="EUR",
            by_service={"SNOWFLAKE INC - Data Cloud": Decimal("42")},
            period=(date(2025, 8, 1), date(2026, 8, 1)),
        )
        assert expense_for_provider("snowflake", report) == Decimal("42")


class TestProvenanceScoping:
    def test_the_note_says_the_number_only_covers_the_scanned_payer(self) -> None:
        report = ExpenseReport(
            currency="EUR", by_service={}, period=(date(2025, 8, 1), date(2026, 8, 1)), payer_accounts=["123456789012"]
        )
        assert "payer" in report.provenance_note.lower()
        assert "123456789012" in report.provenance_note

    def test_the_note_states_the_period(self) -> None:
        report = ExpenseReport(currency="EUR", by_service={}, period=(date(2025, 8, 1), date(2026, 8, 1)))
        assert "2025-08-01" in report.provenance_note

    def test_the_note_names_the_cost_metric(self) -> None:
        report = ExpenseReport(currency="EUR", by_service={}, period=(date(2025, 8, 1), date(2026, 8, 1)))
        assert "unblended" in report.provenance_note.lower()


class TestTaxIsNotAService:
    """Cost Explorer groups tax under SERVICE. Leaving it in makes the column incoherent."""

    def test_it_is_excluded_from_the_total(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", ce_response({"Amazon S3": "100.00", "Tax": "18.00"}))
        stubber.activate()
        report = collect_annual_expense(client=client, today=date(2026, 8, 22))
        assert report.total == Decimal("100.00")

    def test_it_does_not_appear_as_a_service(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", ce_response({"Amazon S3": "100.00", "Tax": "18.00"}))
        stubber.activate()
        assert "Tax" not in collect_annual_expense(client=client, today=date(2026, 8, 22)).by_service

    def test_the_basis_is_stated_rather_than_left_to_be_guessed(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_response("get_cost_and_usage", ce_response({"Amazon S3": "1.00"}))
        stubber.activate()
        assert "net of tax" in collect_annual_expense(client=client, today=date(2026, 8, 22)).provenance_note

    def test_aws_and_a_marketplace_vendor_are_now_on_the_same_basis(self, stubbed) -> None:
        """AWS used to be gross and every seller net, in the same column."""
        client, stubber = stubbed
        rows = {"Amazon S3": "100.00", "Tax": "18.00", "Datadog, Inc. - Pro": "50.00"}
        stubber.add_response("get_cost_and_usage", ce_response(rows))
        stubber.activate()
        report = collect_annual_expense(client=client, today=date(2026, 8, 22))
        assert expense_for_provider("aws", report) == Decimal("150.00")
        assert expense_for_provider("datadog", report) == Decimal("50.00")


class _BuggyClient:
    """Stands in for a client whose call raises something no botocore
    exception hierarchy covers — the shape a real bug in this module's own
    code would take, not a shape AWS ever sends over the wire."""

    def get_cost_and_usage(self, **kwargs: object) -> dict:
        raise ValueError("something nobody anticipated")


class TestCollectAnnualExpenseDegradesRatherThanCrashes:
    """`collect_annual_expense` used to call `readonly()` with no guard of its
    own, so a bare botocore exception (no credentials, a denied permission)
    reached the caller raw and crashed the whole scan instead of degrading to
    a warning. Narrowed to botocore's own exception hierarchy so a genuine bug
    in this function still surfaces as itself."""

    def test_a_bad_profile_raises_awserror_not_a_bare_profilenotfound(self) -> None:
        """boto3 raises `ProfileNotFound` from the Session constructor, before
        any network call — real code, no Stubber, the same shape as
        `collect_marketplace`'s identical test for the identical hazard."""
        with pytest.raises(AwsError):
            collect_annual_expense(profile="dora-roi-test-profile-that-does-not-exist")

    def test_a_denied_call_raises_awserror(self, stubbed) -> None:
        client, stubber = stubbed
        stubber.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException")
        stubber.activate()
        with pytest.raises(AwsError):
            collect_annual_expense(client=client)

    def test_a_non_botocore_exception_is_not_disguised_as_cost_explorer_unavailable(self) -> None:
        """A real bug in this function's own code path — or a client shaped
        nothing like a botocore one — must surface as itself. Reporting it as
        `AwsError("Cost Explorer unavailable")` would bypass the CLI's own
        "this is a bug" path, exactly the failure mode a too-broad catch here
        would reintroduce."""
        with pytest.raises(ValueError, match="something nobody anticipated"):
            collect_annual_expense(client=_BuggyClient())  # type: ignore[arg-type]
