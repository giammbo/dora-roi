"""Federated identity providers: who signs your people in. moto-backed for the
happy paths (moto grants everything); hand-written doubles for the denials it
cannot produce."""

from __future__ import annotations

import boto3
from moto import mock_aws

from dora_roi.collectors.clickops import collect_identity_providers

#: A real SAML metadata shape. `entityID` and the SSO `Location` name the same
#: vendor host twice — deliberately, since that is the common case and the one
#: collect_identity_providers must not double-count. Padded past 1000 bytes
#: with a placeholder in place of a signing certificate: AWS's real API
#: rejects anything shorter (client-side, via botocore's own validator), and a
#: real metadata document is always this long in practice because it carries
#: an X.509 certificate.
_SAML_METADATA = (
    '<?xml version="1.0"?>'
    '<EntityDescriptor entityID="http://www.okta.com/exkabcd1234">'
    "<IDPSSODescriptor><SingleSignOnService "
    'Location="https://acme.okta.com/app/acme_awssso_1/exkabcd1234/sso/saml"/>'
    "<ds:X509Certificate>" + ("x" * 800) + "</ds:X509Certificate>"
    "</IDPSSODescriptor></EntityDescriptor>"
)


class TestIdentityProviders:
    def test_a_saml_provider_names_its_vendor(self) -> None:
        with mock_aws():
            client = boto3.client("iam", region_name="us-east-1")
            client.create_saml_provider(Name="okta-sso", SAMLMetadataDocument=_SAML_METADATA)

            found = collect_identity_providers(client, account_id="111122223333", refused=[])

        assert [p.name for p in found] == ["okta"]
        assert found[0].registry == "aws-iam-idp"
        assert found[0].namespace == "aws"
        assert found[0].resource_count == 1
        assert found[0].source_files == {"aws:iam:111122223333"}

    def test_a_document_naming_the_same_vendor_twice_is_one_provider(self) -> None:
        """entityID and the SSO Location share a host in every real IdP metadata
        document. One SAML provider object must count as one, not two."""
        with mock_aws():
            client = boto3.client("iam", region_name="us-east-1")
            client.create_saml_provider(Name="okta-sso", SAMLMetadataDocument=_SAML_METADATA)

            found = collect_identity_providers(client, account_id="111122223333", refused=[])

        assert found[0].resource_types["saml_provider"] == 1

    def test_an_oidc_provider_names_its_vendor(self) -> None:
        with mock_aws():
            client = boto3.client("iam", region_name="us-east-1")
            client.create_open_id_connect_provider(
                Url="https://token.actions.githubusercontent.com", ClientIDList=["sts.amazonaws.com"]
            )

            found = collect_identity_providers(client, account_id="111122223333", refused=[])

        assert [p.name for p in found] == ["github"]
        assert found[0].resource_types["oidc_provider"] == 1

    def test_no_providers_is_no_vendors_and_no_refusal(self) -> None:
        with mock_aws():
            client = boto3.client("iam", region_name="us-east-1")
            refused: list[str] = []
            found = collect_identity_providers(client, account_id="111122223333", refused=refused)

        assert found == []
        assert refused == []

    def test_an_unknown_host_produces_nothing_rather_than_a_guess(self) -> None:
        with mock_aws():
            client = boto3.client("iam", region_name="us-east-1")
            client.create_open_id_connect_provider(
                Url="https://sso.internal.example.com", ClientIDList=["sts.amazonaws.com"]
            )

            found = collect_identity_providers(client, account_id="111122223333", refused=[])

        assert found == []

    def test_both_channels_merge_under_one_vendor(self) -> None:
        """A SAML provider and an OIDC provider for the same vendor are one row."""
        with mock_aws():
            client = boto3.client("iam", region_name="us-east-1")
            client.create_saml_provider(Name="okta-sso", SAMLMetadataDocument=_SAML_METADATA)
            client.create_open_id_connect_provider(Url="https://oauth2.okta.com", ClientIDList=["sts.amazonaws.com"])

            found = collect_identity_providers(client, account_id="111122223333", refused=[])

        assert [p.name for p in found] == ["okta"]
        assert found[0].resource_types["saml_provider"] == 1
        assert found[0].resource_types["oidc_provider"] == 1
        assert found[0].resource_count == 2


class _DeniedOnList:
    """Neither `List*` call is permitted. The scan carries on regardless."""

    def list_saml_providers(self, **kwargs: object) -> None:
        raise RuntimeError("AccessDenied: iam:ListSAMLProviders")

    def list_open_id_connect_providers(self, **kwargs: object) -> None:
        raise RuntimeError("AccessDenied: iam:ListOpenIDConnectProviders")


class _DeniedOnGet:
    """Listing works; reading the one provider found does not."""

    def list_saml_providers(self, **kwargs: object) -> dict:
        return {"SAMLProviderList": [{"Arn": "arn:aws:iam::111122223333:saml-provider/okta-sso"}]}

    def get_saml_provider(self, **kwargs: object) -> None:
        raise RuntimeError("AccessDenied: iam:GetSAMLProvider")

    def list_open_id_connect_providers(self, **kwargs: object) -> dict:
        return {"OpenIDConnectProviderList": []}


class _DeniedOnGetOidc:
    """Listing works for OIDC; reading the one provider found does not — the
    OIDC-side mirror of `_DeniedOnGet`."""

    def list_saml_providers(self, **kwargs: object) -> dict:
        return {"SAMLProviderList": []}

    def list_open_id_connect_providers(self, **kwargs: object) -> dict:
        return {
            "OpenIDConnectProviderList": [
                {"Arn": "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"}
            ]
        }

    def get_open_id_connect_provider(self, **kwargs: object) -> None:
        raise RuntimeError("AccessDenied: iam:GetOpenIDConnectProvider")


class _BareOidcUrl:
    """AWS's `GetOpenIDConnectProvider` can return the issuer URL without a
    scheme even though `CreateOpenIDConnectProvider` requires one on the way
    in — moto enforces the same client-side validation real botocore does, so
    this shape can only be produced with a double, not by creating one for
    real."""

    def list_saml_providers(self, **kwargs: object) -> dict:
        return {"SAMLProviderList": []}

    def list_open_id_connect_providers(self, **kwargs: object) -> dict:
        return {
            "OpenIDConnectProviderList": [
                {"Arn": "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"}
            ]
        }

    def get_open_id_connect_provider(self, **kwargs: object) -> dict:
        return {"Url": "token.actions.githubusercontent.com"}


class TestSchemeHandling:
    def test_an_oidc_url_without_a_scheme_still_resolves(self) -> None:
        found = collect_identity_providers(_BareOidcUrl(), account_id="111122223333", refused=[])

        assert [p.name for p in found] == ["github"]


class TestDeniedPermissions:
    def test_a_denied_listing_is_refused_not_raised(self) -> None:
        refused: list[str] = []
        found = collect_identity_providers(_DeniedOnList(), account_id="111122223333", refused=refused)

        assert found == []
        assert len(refused) == 2
        assert any("ListSAMLProviders" in line for line in refused)
        assert any("ListOpenIDConnectProviders" in line for line in refused)

    def test_the_refusal_names_the_account(self) -> None:
        refused: list[str] = []
        collect_identity_providers(_DeniedOnList(), account_id="111122223333", refused=refused)

        assert all("111122223333" in line for line in refused)

    def test_a_denied_read_on_one_provider_is_refused_not_raised(self) -> None:
        refused: list[str] = []
        found = collect_identity_providers(_DeniedOnGet(), account_id="111122223333", refused=refused)

        assert found == []
        assert len(refused) == 1
        assert "GetSAMLProvider" in refused[0]

    def test_a_denied_read_on_one_oidc_provider_is_refused_not_raised(self) -> None:
        refused: list[str] = []
        found = collect_identity_providers(_DeniedOnGetOidc(), account_id="111122223333", refused=refused)

        assert found == []
        assert len(refused) == 1
        assert "GetOpenIDConnectProvider" in refused[0]
