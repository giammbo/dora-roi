"""EventBridge partner sources: a SaaS pushing events straight into your bus.

Hand-written doubles throughout, not moto: moto has no EventBridge
`list_event_sources` support for partner sources (there is nothing to create
one with — a partner source only ever appears after the partner's own backend
calls `PutPartnerEvents`, which is not something a customer account can do),
so a real client can never be put in the state these tests need.
"""

from __future__ import annotations

from dora_roi.collectors.clickops import collect_partner_event_sources


class _Bus:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_event_sources(self, **kwargs: object) -> dict:
        return {"EventSources": [{"Name": n} for n in self._names]}


class _Denied:
    def list_event_sources(self, **kwargs: object) -> None:
        raise RuntimeError("AccessDenied: events:ListEventSources")


class TestPartnerEventSources:
    def test_a_partner_source_names_its_vendor(self) -> None:
        client = _Bus(["aws.partner/datadoghq.com/12345/events"])

        found = collect_partner_event_sources(client, account_id="111122223333", region="eu-west-1", refused=[])

        assert [p.name for p in found] == ["datadog"]
        assert found[0].registry == "aws-eventbridge"
        assert found[0].namespace == "aws"
        assert found[0].source_files == {"aws:events:111122223333:eu-west-1"}
        assert found[0].regions == {"eu-west-1"}
        assert found[0].resource_count == 1
        assert found[0].resource_types["partner_event_source"] == 1

    def test_a_non_partner_source_is_not_a_vendor(self) -> None:
        """`default` is every account's own bus, not a SaaS integration."""
        client = _Bus(["default"])

        found = collect_partner_event_sources(client, account_id="111122223333", region="eu-west-1", refused=[])

        assert found == []

    def test_no_sources_is_no_vendors_and_no_refusal(self) -> None:
        refused: list[str] = []
        client = _Bus([])

        found = collect_partner_event_sources(client, account_id="1", region="eu-west-1", refused=refused)

        assert found == []
        assert refused == []

    def test_an_unknown_host_produces_nothing_rather_than_a_guess(self) -> None:
        """A partner-shaped name whose host the table does not know must not
        invent a vendor — the same rule `collect_identity_providers` follows
        for an unrecognised SAML/OIDC host."""
        client = _Bus(["aws.partner/unknown-saas.example/12345/events"])

        found = collect_partner_event_sources(client, account_id="111122223333", region="eu-west-1", refused=[])

        assert found == []

    def test_two_sources_for_the_same_vendor_are_one_row(self) -> None:
        """Two Datadog event sources (one per Datadog org, say) are one
        vendor relationship, not two — same rule as the IdP channel's
        one-document-two-hostnames case, applied to two API objects instead
        of two regex hits."""
        client = _Bus(
            [
                "aws.partner/datadoghq.com/111122223333/eu-events",
                "aws.partner/datadoghq.com/111122223333/us-events",
            ]
        )

        found = collect_partner_event_sources(client, account_id="111122223333", region="eu-west-1", refused=[])

        assert [p.name for p in found] == ["datadog"]
        assert found[0].resource_count == 2
        assert found[0].resource_types["partner_event_source"] == 2

    def test_several_vendors_are_sorted_by_name(self) -> None:
        client = _Bus(
            [
                "aws.partner/segment.com/111122223333/events",
                "aws.partner/datadoghq.com/111122223333/events",
            ]
        )

        found = collect_partner_event_sources(client, account_id="111122223333", region="eu-west-1", refused=[])

        assert [p.name for p in found] == ["datadog", "segment"]

    def test_a_mix_of_partner_and_non_partner_sources_only_reports_the_partner(self) -> None:
        client = _Bus(["default", "aws.partner/datadoghq.com/111122223333/events"])

        found = collect_partner_event_sources(client, account_id="111122223333", region="eu-west-1", refused=[])

        assert [p.name for p in found] == ["datadog"]


class TestDeniedPermissions:
    def test_a_denied_listing_is_refused_not_raised(self) -> None:
        refused: list[str] = []

        found = collect_partner_event_sources(_Denied(), account_id="1", region="eu-west-1", refused=refused)

        assert found == []
        assert len(refused) == 1
        assert "events:ListEventSources" in refused[0]

    def test_the_refusal_names_the_account_and_the_region(self) -> None:
        """Regional, unlike the IdP channel's account-wide refusal: the same
        account denied in two regions must produce two distinguishable lines,
        not one that could be either."""
        refused: list[str] = []

        collect_partner_event_sources(_Denied(), account_id="111122223333", region="ap-southeast-1", refused=refused)

        assert "111122223333" in refused[0]
        assert "ap-southeast-1" in refused[0]


class _PaginatedBus:
    """Two pages of `list_event_sources`, chained by EventBridge's own
    `NextToken` — the API paginates for real, unlike `_Bus`'s single-call
    fixtures above, so `_all_event_sources` must walk to the end of it before
    this channel decides the bus has only whatever page one carried."""

    def list_event_sources(self, **kwargs: object) -> dict:
        if kwargs.get("NextToken") is None:
            return {
                "EventSources": [{"Name": "aws.partner/datadoghq.com/111122223333/events"}],
                "NextToken": "page-2",
            }
        assert kwargs["NextToken"] == "page-2"
        return {"EventSources": [{"Name": "aws.partner/segment.com/111122223333/events"}]}


class TestPagination:
    def test_a_second_page_of_event_sources_is_not_dropped(self) -> None:
        """A partner integration sitting on page two must not vanish as
        though the bus only ever had the one on page one."""
        found = collect_partner_event_sources(
            _PaginatedBus(), account_id="111122223333", region="eu-west-1", refused=[]
        )

        assert [p.name for p in found] == ["datadog", "segment"]
