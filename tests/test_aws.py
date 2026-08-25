"""AWS Organizations collector. moto-backed: no real AWS, ever."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from dora_roi.collectors.aws import (
    READ_ONLY_ACTIONS,
    AwsError,
    DiscoveredAccount,
    collect_organization,
    readonly,
)


@pytest.fixture
def organization():
    """A three-account org: one in Production, one in Sandbox, the payer at the root."""
    with mock_aws():
        client = boto3.client("organizations", region_name="eu-south-1")
        client.create_organization(FeatureSet="ALL")
        root = client.list_roots()["Roots"][0]["Id"]
        production = client.create_organizational_unit(ParentId=root, Name="Production")["OrganizationalUnit"]["Id"]
        sandbox = client.create_organizational_unit(ParentId=root, Name="Sandbox")["OrganizationalUnit"]["Id"]
        prod_account = client.create_account(AccountName="acme-prod", Email="prod@acme.example")["CreateAccountStatus"][
            "AccountId"
        ]
        sand_account = client.create_account(AccountName="acme-sandbox", Email="sbx@acme.example")[
            "CreateAccountStatus"
        ]["AccountId"]
        client.move_account(AccountId=prod_account, SourceParentId=root, DestinationParentId=production)
        client.move_account(AccountId=sand_account, SourceParentId=root, DestinationParentId=sandbox)
        yield client


class TestReadOnlyGuard:
    """Golden rule 2, enforced rather than promised."""

    def test_a_mutating_call_is_refused_before_it_leaves(self) -> None:
        with mock_aws():
            client = boto3.client("organizations", region_name="eu-south-1")
            with pytest.raises(AwsError, match="read-only"):
                readonly(client, "create_account", AccountName="x", Email="x@y.z")

    def test_the_refusal_names_the_operation(self) -> None:
        with mock_aws():
            client = boto3.client("organizations", region_name="eu-south-1")
            with pytest.raises(AwsError, match="delete_organization"):
                readonly(client, "delete_organization")

    def test_list_describe_and_get_are_allowed(self, organization) -> None:
        assert readonly(organization, "list_roots")["Roots"]
        assert readonly(organization, "describe_organization")["Organization"]

    def test_every_documented_action_is_a_read(self) -> None:
        assert all(
            action.split(":", 1)[1].startswith(("List", "Describe", "Get", "Search")) for action in READ_ONLY_ACTIONS
        )


class TestCollectOrganization:
    def test_finds_every_account(self, organization) -> None:
        inventory = collect_organization(client=organization)
        assert {a.name for a in inventory.accounts} == {"master", "acme-prod", "acme-sandbox"}

    def test_records_the_organization_id(self, organization) -> None:
        inventory = collect_organization(client=organization)
        assert inventory.organization_id.startswith("o-")

    def test_hierarchy_is_the_ou_path(self, organization) -> None:
        by_name = {a.name: a for a in collect_organization(client=organization).accounts}
        assert by_name["acme-prod"].ou_path == ("Root", "Production")
        assert by_name["acme-sandbox"].ou_path == ("Root", "Sandbox")

    def test_an_account_at_the_root_has_a_one_element_path(self, organization) -> None:
        by_name = {a.name: a for a in collect_organization(client=organization).accounts}
        assert by_name["master"].ou_path == ("Root",)

    def test_accounts_are_sorted_by_path_then_name(self, organization) -> None:
        accounts = collect_organization(client=organization).accounts
        assert accounts == sorted(accounts, key=lambda a: (a.ou_path, a.name))

    def test_carries_the_account_id_and_status(self, organization) -> None:
        account = collect_organization(client=organization).accounts[0]
        assert account.account_id.isdigit()
        assert account.status == "ACTIVE"

    def test_nested_ous_are_walked(self, organization) -> None:
        root = organization.list_roots()["Roots"][0]["Id"]
        production = next(
            ou["Id"]
            for ou in organization.list_organizational_units_for_parent(ParentId=root)["OrganizationalUnits"]
            if ou["Name"] == "Production"
        )
        nested = organization.create_organizational_unit(ParentId=production, Name="Payments")["OrganizationalUnit"][
            "Id"
        ]
        account = organization.create_account(AccountName="acme-pay", Email="pay@acme.example")["CreateAccountStatus"][
            "AccountId"
        ]
        organization.move_account(AccountId=account, SourceParentId=root, DestinationParentId=nested)

        by_name = {a.name: a for a in collect_organization(client=organization).accounts}
        assert by_name["acme-pay"].ou_path == ("Root", "Production", "Payments")


class TestB0102Hints:
    """Names are AUTO-as-hint, hierarchy is SEMI. Neither is a legal fact."""

    def test_a_hint_is_never_authoritative(self, organization) -> None:
        for account in collect_organization(client=organization).accounts:
            hint = account.as_b0102_hint()
            assert hint["0020"] == account.name
            assert hint["0050"] == " / ".join(account.ou_path)

    def test_the_hint_carries_no_lei(self, organization) -> None:
        hint = collect_organization(client=organization).accounts[0].as_b0102_hint()
        assert "0010" not in hint

    def test_an_aws_account_is_not_a_legal_entity(self, organization) -> None:
        account = collect_organization(client=organization).accounts[0]
        assert "not a legal entity" in account.caveat.lower()


class TestErrors:
    def test_no_organization_is_an_actionable_error(self) -> None:
        with mock_aws():
            client = boto3.client("organizations", region_name="eu-south-1")
            with pytest.raises(AwsError, match="organization"):
                collect_organization(client=client)

    def test_the_error_mentions_the_permission_needed(self) -> None:
        with mock_aws():
            client = boto3.client("organizations", region_name="eu-south-1")
            with pytest.raises(AwsError) as exc:
                collect_organization(client=client)
            assert "organizations:" in str(exc.value)


def test_discovered_account_is_frozen() -> None:
    account = DiscoveredAccount(account_id="1", name="x", email=None, status="ACTIVE", ou_path=("Root",))
    with pytest.raises(AttributeError):
        account.name = "y"  # type: ignore[misc]
