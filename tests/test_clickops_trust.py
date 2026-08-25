"""Cross-account trust: who can walk into this account without asking.

moto-backed for the happy paths (moto validates `AssumeRolePolicyDocument` at
`create_role` and produces well-formed JSON every time), a hand-written fake
client for the one case moto cannot produce: a role whose trust policy is
unreadable garbage. That shape can only reach this code from a stale role
created years before a policy schema tightened, or from a raw API response
this module chose not to trust blindly — moto's own validation makes it
impossible to construct via `create_role`, so the fake is not a shortcut, it
is the only way to exercise the parse-failure path at all.
"""

from __future__ import annotations

import json

import boto3
from moto import mock_aws

from dora_roi.collectors.clickops import collect_trust_relationships

OWN = frozenset({"111122223333", "444455556666"})


def _trust(principal: str, external_id: str | None = None) -> str:
    statement: dict = {
        "Effect": "Allow",
        "Principal": {"AWS": principal},
        "Action": "sts:AssumeRole",
    }
    if external_id:
        statement["Condition"] = {"StringEquals": {"sts:ExternalId": external_id}}
    return json.dumps({"Version": "2012-10-17", "Statement": [statement]})


@mock_aws
class TestTrustRelationships:
    def _iam(self):
        return boto3.client("iam", region_name="us-east-1")

    def test_a_known_vendor_account_becomes_a_provider(self) -> None:
        client = self._iam()
        client.create_role(
            RoleName="DatadogIntegrationRole",
            AssumeRolePolicyDocument=_trust("arn:aws:iam::464622532012:root", external_id="abc123"),
        )

        providers, unknown = collect_trust_relationships(
            client, account_id="111122223333", own_accounts=OWN, refused=[]
        )

        assert [p.name for p in providers] == ["datadog"]
        assert providers[0].registry == "aws-trust"
        assert providers[0].namespace == "aws"
        assert providers[0].source_files == {"aws:iam:111122223333"}
        assert unknown == []

    def test_an_unknown_external_account_never_becomes_a_vendor(self) -> None:
        """Golden rule 1 for this channel: no invented names, ever."""
        client = self._iam()
        client.create_role(RoleName="MysteryRole", AssumeRolePolicyDocument=_trust("arn:aws:iam::999988887777:root"))

        providers, unknown = collect_trust_relationships(
            client, account_id="111122223333", own_accounts=OWN, refused=[]
        )

        assert providers == []
        assert [u.account_id for u in unknown] == ["999988887777"]
        assert unknown[0].role_name == "MysteryRole"
        assert unknown[0].has_external_id is False

    def test_a_role_trusted_by_your_own_account_is_not_external(self) -> None:
        client = self._iam()
        client.create_role(RoleName="Internal", AssumeRolePolicyDocument=_trust("arn:aws:iam::444455556666:root"))
        assert collect_trust_relationships(client, account_id="111122223333", own_accounts=OWN, refused=[]) == ([], [])

    def test_a_role_trusted_by_the_scanned_account_itself_is_not_external(self) -> None:
        """`own_accounts` covers the rest of the org; the account being scanned
        right now must also never show up as its own external principal."""
        client = self._iam()
        client.create_role(RoleName="SelfTrust", AssumeRolePolicyDocument=_trust("arn:aws:iam::111122223333:root"))
        assert collect_trust_relationships(client, account_id="111122223333", own_accounts=OWN, refused=[]) == ([], [])

    def test_a_service_principal_is_aws_itself_not_a_third_party(self) -> None:
        client = self._iam()
        client.create_role(
            RoleName="LambdaRole",
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )
        assert collect_trust_relationships(client, account_id="111122223333", own_accounts=OWN, refused=[]) == ([], [])

    def test_a_deny_statement_trusts_nobody(self) -> None:
        """An explicit `Deny` names a principal without trusting it. Crediting
        a vendor, or flagging an unknown, from a statement that locks someone
        out would be exactly backwards."""
        client = self._iam()
        client.create_role(
            RoleName="MostlyDenied",
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Deny",
                            "Principal": {"AWS": "arn:aws:iam::999988887777:root"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )
        assert collect_trust_relationships(client, account_id="111122223333", own_accounts=OWN, refused=[]) == ([], [])

    def test_an_external_id_is_recorded_as_evidence_strength(self) -> None:
        client = self._iam()
        client.create_role(
            RoleName="VendorRole", AssumeRolePolicyDocument=_trust("arn:aws:iam::999988887777:root", "xyz")
        )
        _, unknown = collect_trust_relationships(client, account_id="111122223333", own_accounts=OWN, refused=[])
        assert unknown[0].has_external_id is True

    def test_two_roles_trusting_the_same_vendor_are_one_provider(self) -> None:
        client = self._iam()
        client.create_role(RoleName="DatadogRoleOne", AssumeRolePolicyDocument=_trust("arn:aws:iam::464622532012:root"))
        client.create_role(RoleName="DatadogRoleTwo", AssumeRolePolicyDocument=_trust("arn:aws:iam::464622532012:root"))

        providers, _ = collect_trust_relationships(client, account_id="111122223333", own_accounts=OWN, refused=[])

        assert [p.name for p in providers] == ["datadog"]
        assert providers[0].resource_types["assume_role_trust"] == 2

    def test_no_roles_is_no_providers_and_no_unknowns(self) -> None:
        client = self._iam()
        providers, unknown = collect_trust_relationships(
            client, account_id="111122223333", own_accounts=OWN, refused=[]
        )
        assert (providers, unknown) == ([], [])


class _Denied:
    def list_roles(self, **kwargs: object) -> None:
        raise RuntimeError("AccessDenied: iam:ListRoles")


def test_a_denied_listing_is_refused_not_raised() -> None:
    refused: list[str] = []
    assert collect_trust_relationships(_Denied(), account_id="1", own_accounts=OWN, refused=refused) == ([], [])
    assert refused and "iam:ListRoles" in refused[0]


class _OneMalformedOneGood:
    """`list_roles` answers directly, no API call moto could ever be asked to
    validate: one role carries a trust policy that is not JSON at all (the
    shape a 2019-era manually-edited policy, or a raw API response this
    module chooses not to trust blindly, could produce), the other a normal
    Datadog trust. Standing in for real IAM here is required, not incidental
    — moto's `create_role` runs botocore's own client-side policy validator
    and rejects garbage before it ever reaches this module's parser.
    """

    def list_roles(self, **kwargs: object) -> dict:
        return {
            "Roles": [
                {"RoleName": "Weird2019", "AssumeRolePolicyDocument": "{not valid json"},
                {"RoleName": "Good", "AssumeRolePolicyDocument": _trust("arn:aws:iam::464622532012:root")},
            ]
        }


def test_a_malformed_trust_policy_skips_the_role_not_the_scan() -> None:
    """One weird policy from 2019 cannot be the reason you get no register."""
    refused: list[str] = []
    providers, unknown = collect_trust_relationships(
        _OneMalformedOneGood(), account_id="111122223333", own_accounts=OWN, refused=refused
    )

    assert [p.name for p in providers] == ["datadog"]
    assert unknown == []
    assert refused and "Weird2019" in refused[0]


class _EdgeShapedPolicies:
    """Three shapes this module's parser must survive, none of which needs
    (or, for the second one, could even get) moto's blessing: a principal
    given as a bare 12-digit account with no ARN wrapper at all (IAM accepts
    both forms, and `_account_of` must too); a `Principal` that is not a dict
    in the first place (a literal `"*"`, which IAM itself refuses in a real
    `AssumeRolePolicyDocument` — so a fake, not moto, is the only way to see
    how this module's own parser reacts rather than how IAM's validator
    would); and a `Condition.StringEquals` that is not a dict either. None of
    these should crash the scan or the one role carrying it.
    """

    def list_roles(self, **kwargs: object) -> dict:
        return {
            "Roles": [
                {
                    "RoleName": "BareAccountRole",
                    "AssumeRolePolicyDocument": json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {"Effect": "Allow", "Principal": {"AWS": "999988887777"}, "Action": "sts:AssumeRole"}
                            ],
                        }
                    ),
                },
                {
                    "RoleName": "WildcardPrincipalRole",
                    "AssumeRolePolicyDocument": json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [{"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}],
                        }
                    ),
                },
                {
                    "RoleName": "OddConditionRole",
                    "AssumeRolePolicyDocument": json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Principal": {"AWS": "arn:aws:iam::888877776666:root"},
                                    "Action": "sts:AssumeRole",
                                    "Condition": {"StringEquals": "not-a-dict"},
                                }
                            ],
                        }
                    ),
                },
            ]
        }


def test_edge_shaped_principals_are_handled_without_crashing() -> None:
    providers, unknown = collect_trust_relationships(
        _EdgeShapedPolicies(), account_id="111122223333", own_accounts=OWN, refused=[]
    )

    assert providers == []
    accounts = {u.account_id for u in unknown}
    assert accounts == {"999988887777", "888877776666"}
    bare = next(u for u in unknown if u.account_id == "999988887777")
    assert bare.role_name == "BareAccountRole"
    odd_condition = next(u for u in unknown if u.account_id == "888877776666")
    assert odd_condition.has_external_id is False


def test_every_vendor_account_maps_to_a_known_provider() -> None:
    """An account pointing at a key the mapping does not know produces a row
    the register cannot use — the same rule the domain table already obeys."""
    from dora_roi.collectors.clickops import _vendor_accounts
    from dora_roi.enrichment.mapping import load_mapping

    mapping = load_mapping()
    assert {v for v in _vendor_accounts().values() if v not in mapping} == set()
