"""The sweep, and where an authoritative fact becomes a FILLED field."""

from __future__ import annotations

from decimal import Decimal

import pytest
from moto import mock_aws

from dora_roi.cli import _apply_vendor_facts
from dora_roi.collectors.aws import AwsError
from dora_roi.collectors.clickops import VendorFact, _session, collect_clickops
from dora_roi.models.enums import FieldStatus
from dora_roi.models.templates import ThirdPartyProvider


def test_a_marketplace_fact_fills_the_legal_name() -> None:
    row = ThirdPartyProvider(source_key="datadog")
    row.legal_name = "datadog"
    row.mark("legal_name", FieldStatus.INFERRED, source="mapping")

    _apply_vendor_facts(
        [row],
        [
            VendorFact(
                key="datadog", legal_name="Datadog, Inc.", annual_spend=Decimal("4200"), currency="USD", source="aws:ce"
            )
        ],
        [],
    )

    assert row.legal_name == "Datadog, Inc."
    assert row.status_of("legal_name") is FieldStatus.FILLED


def test_a_fact_for_a_vendor_with_no_row_changes_nothing() -> None:
    row = ThirdPartyProvider(source_key="aws")
    _apply_vendor_facts(
        [row],
        [VendorFact(key="snyk", legal_name="Snyk Limited", annual_spend=None, currency=None, source="aws:ce")],
        [],
    )
    assert row.status_of("legal_name") is not FieldStatus.FILLED


def test_the_overlay_still_wins_over_a_billing_fact() -> None:
    """A human assertion outranks a billing API: the overlay is the contract."""
    row = ThirdPartyProvider(source_key="datadog")
    row.legal_name = "Datadog International Ltd"
    row.mark("legal_name", FieldStatus.FILLED, source="overlay")

    _apply_vendor_facts(
        [row],
        [VendorFact(key="datadog", legal_name="Datadog, Inc.", annual_spend=None, currency=None, source="aws:ce")],
        [],
    )

    assert row.legal_name == "Datadog International Ltd"


def test_two_facts_under_one_key_fill_nothing_and_say_so() -> None:
    """C1, second line of defence. `collect_marketplace` refuses this pair at
    the source, so nothing in the shipped pipeline reaches here — but a
    `{fact.key: fact}` comprehension would resolve it by iteration order if a
    second producer of facts ever appeared, which is the defect itself."""
    row = ThirdPartyProvider(source_key="datadog")
    refused: list[str] = []

    _apply_vendor_facts(
        [row],
        [
            VendorFact(
                key="datadog", legal_name="Datadog, Inc.", annual_spend=Decimal("4200"), currency="USD", source="aws:ce"
            ),
            VendorFact(
                key="datadog",
                legal_name="Datadog International Ltd",
                annual_spend=Decimal("99"),
                currency="USD",
                source="aws:ce",
            ),
        ],
        refused,
    )

    assert row.status_of("legal_name") is not FieldStatus.FILLED
    assert row.status_of("total_annual_expense") is not FieldStatus.FILLED
    assert row.total_annual_expense is None
    assert refused and "Datadog, Inc." in refused[0] and "Datadog International Ltd" in refused[0]


def test_the_same_fact_arriving_twice_is_not_a_collision() -> None:
    """The negative case: one seller, one key, applied — not withheld because
    the list happened to carry it twice."""
    row = ThirdPartyProvider(source_key="datadog")
    fact = VendorFact(
        key="datadog", legal_name="Datadog, Inc.", annual_spend=Decimal("4200"), currency="USD", source="aws:ce"
    )
    refused: list[str] = []

    _apply_vendor_facts([row], [fact, fact], refused)

    assert row.status_of("legal_name") is FieldStatus.FILLED
    assert row.total_annual_expense == Decimal("4200")
    assert refused == []


#: A real SAML metadata shape, same as tests/test_clickops_idp.py: padded past
#: 1000 bytes because AWS's own client-side validator rejects anything shorter.
_SAML_METADATA = (
    '<?xml version="1.0"?>'
    '<EntityDescriptor entityID="http://www.okta.com/exkabcd1234">'
    "<IDPSSODescriptor><SingleSignOnService "
    'Location="https://acme.okta.com/app/acme_awssso_1/exkabcd1234/sso/saml"/>'
    "<ds:X509Certificate>" + ("x" * 800) + "</ds:X509Certificate>"
    "</IDPSSODescriptor></EntityDescriptor>"
)

_DATADOG_TRUST = (
    '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
    '"Principal": {"AWS": "arn:aws:iam::464622532012:root"}, "Action": "sts:AssumeRole"}]}'
)


@mock_aws
class TestCollectClickops:
    """The per-account orchestrator: one session, every account-scoped channel."""

    def test_aggregates_every_channel_for_one_account(self) -> None:
        import boto3

        iam = boto3.client("iam", region_name="us-east-1")
        iam.create_saml_provider(Name="okta-sso", SAMLMetadataDocument=_SAML_METADATA)
        iam.create_role(RoleName="DatadogIntegrationRole", AssumeRolePolicyDocument=_DATADOG_TRUST)

        # No regions here on purpose: moto has no `list_event_sources` support
        # at all (see tests/test_clickops_eventbridge.py), so asking for one
        # would only exercise moto's own gap, not this function. The region
        # loop itself is covered separately, below.
        refused: list[str] = []
        providers, unknown = collect_clickops(
            profile=None,
            role_arn=None,
            account_id="111122223333",
            own_accounts=frozenset({"111122223333"}),
            regions=frozenset(),
            refused=refused,
        )

        # okta from the identity-provider channel, datadog from the trust
        # channel — one session served both, and the caller never had to know
        # that two different IAM calls were involved.
        assert {p.name for p in providers} == {"okta", "datadog"}
        assert unknown == []
        assert refused == []

    def test_an_unnamed_trusted_account_is_reported_not_dropped(self) -> None:
        import boto3

        iam = boto3.client("iam", region_name="us-east-1")
        iam.create_role(
            RoleName="MysteryRole",
            AssumeRolePolicyDocument=(
                '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
                '"Principal": {"AWS": "999988887777"}, "Action": "sts:AssumeRole"}]}'
            ),
        )

        _, unknown = collect_clickops(
            profile=None,
            role_arn=None,
            account_id="111122223333",
            own_accounts=frozenset({"111122223333"}),
            regions=frozenset(),
            refused=[],
        )

        assert [u.account_id for u in unknown] == ["999988887777"]

    def test_sweeps_every_region_it_is_given(self) -> None:
        """Regional channels (EventBridge) get one client per region, not one for all."""
        providers, _ = collect_clickops(
            profile=None,
            role_arn=None,
            account_id="111122223333",
            own_accounts=frozenset(),
            regions=frozenset({"eu-west-1", "us-east-1"}),
            refused=[],
        )
        assert providers == []  # no partner sources exist; the point is that nothing raised per-region


def test_a_typo_d_profile_costs_the_account_not_the_sweep() -> None:
    """The real failure C3 was about: no mock of `_session`, no mock of
    `boto3`. `boto3.Session(profile_name=...)` raises `ProfileNotFound`
    **eagerly, from the constructor** for a profile that is not in the local
    AWS config — verified directly above, in the container this suite runs
    in. A test that instead monkeypatches `_session` to raise `AwsError`
    proves `collect_clickops` can catch an `AwsError`; it says nothing about
    whether a mistyped profile ever produces one, which is exactly the gap
    that let this bug ship. A per-account config typo must cost this one
    account, never the rest of an N-account sweep."""
    refused: list[str] = []

    providers, unknown = collect_clickops(
        profile="definitely-not-a-real-profile-xyz",
        role_arn=None,
        account_id="111122223333",
        own_accounts=frozenset(),
        regions=frozenset(),
        refused=refused,
    )

    assert (providers, unknown) == ([], [])
    assert refused and "111122223333" in refused[0]


def test_a_broken_session_costs_the_account_not_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Complements the real test above: whatever kind of denial `_session`
    itself raises (`AwsError` or `ClickopsError`), `collect_clickops`'s own
    contract — catch it, refuse this account, return an empty result — holds
    regardless of which internal branch of `_session` produced it."""
    import dora_roi.collectors.clickops as clickops_module

    def boom(**_: object) -> None:
        raise AwsError("could not assume arn:aws:iam::999999999999:role/Nope: AccessDenied")

    monkeypatch.setattr(clickops_module, "_session", boom)
    refused: list[str] = []

    providers, unknown = collect_clickops(
        profile=None,
        role_arn="arn:aws:iam::999999999999:role/Nope",
        account_id="111122223333",
        own_accounts=frozenset(),
        regions=frozenset(),
        refused=refused,
    )

    assert (providers, unknown) == ([], [])
    assert refused and "111122223333" in refused[0] and "AccessDenied" in refused[0]


@mock_aws
class TestSession:
    def test_no_role_returns_a_plain_profile_session(self) -> None:
        session = _session(profile=None, role_arn=None)
        identity = session.client("sts", region_name="us-east-1").get_caller_identity()
        assert identity["Account"]

    def test_assuming_a_role_returns_a_session_with_that_roles_credentials(self) -> None:
        import boto3

        iam = boto3.client("iam", region_name="us-east-1")
        iam.create_role(
            RoleName="ReadOnlySweep",
            AssumeRolePolicyDocument=(
                '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
                '"Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]}'
            ),
        )
        role_arn = iam.get_role(RoleName="ReadOnlySweep")["Role"]["Arn"]

        session = _session(profile=None, role_arn=role_arn)

        identity = session.client("sts", region_name="us-east-1").get_caller_identity()
        assert "assumed-role" in identity["Arn"]

    def test_an_unassumable_role_is_an_awserror_not_a_crash(self) -> None:
        # Too short to pass STS's own client-side parameter validation
        # (RoleArn has a 20-character minimum) — deterministic, no moto
        # leniency involved.
        with pytest.raises(AwsError):
            _session(profile=None, role_arn="short")
