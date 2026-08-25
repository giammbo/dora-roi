"""Provider mapping loader and B_05.01 row construction (T5)."""

from pathlib import Path

import pytest

from dora_roi.collectors.tfstate import DiscoveredProvider
from dora_roi.enrichment.mapping import (
    MappingError,
    ProviderMapping,
    load_mapping,
    provider_to_tpp,
)
from dora_roi.models.enums import FieldStatus, ICTServiceType, TypeOfPerson

REQUIRED_PROVIDERS = {
    "aws",
    "google",
    "azurerm",
    "azuread",
    "datadog",
    "cloudflare",
    "snowflake",
    "mongodbatlas",
    "github",
    "gitlab",
    "okta",
    "auth0",
    "pagerduty",
    "fastly",
    "newrelic",
    "confluent",
    "vault",
    "tfe",
    "buildkite",
    "elastic",
    "grafana",
    "sentry",
    "vercel",
    "digitalocean",
    "scaleway",
    "ovh",
}


def discovered(name: str = "aws", **kw: object) -> DiscoveredProvider:
    defaults = {"namespace": "hashicorp", "registry": "registry.terraform.io", "resource_count": 5}
    return DiscoveredProvider(name=name, **{**defaults, **kw})  # type: ignore[arg-type]


class TestPackagedDefaults:
    def test_defaults_load(self) -> None:
        mapping = load_mapping()
        assert len(mapping) >= 20

    def test_ships_the_providers_the_plan_lists(self) -> None:
        assert REQUIRED_PROVIDERS <= set(load_mapping())

    def test_every_entry_is_complete(self) -> None:
        for name, entry in load_mapping().items():
            assert entry.vendor.strip(), name
            assert len(entry.hq_country) == 2, name
            assert entry.services, name

    def test_service_codes_are_parsed_into_the_closed_list(self) -> None:
        assert ICTServiceType.S17 in load_mapping()["aws"].services

    def test_hq_country_is_iso2_upper(self) -> None:
        assert load_mapping()["scaleway"].hq_country == "FR"


class TestUserOverride:
    def test_override_one_key_and_keep_the_rest(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("aws:\n  hq_country: US\n")
        entry = load_mapping(user)["aws"]
        assert entry.hq_country == "US"
        assert entry.vendor == "Amazon Web Services EMEA SARL"
        assert ICTServiceType.S17 in entry.services

    def test_user_can_replace_the_service_list(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("aws:\n  services: [S09]\n")
        assert load_mapping(user)["aws"].services == [ICTServiceType.S09]

    def test_user_can_add_a_provider(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("acme:\n  vendor: Acme SpA\n  hq_country: IT\n  services: [S19]\n")
        mapping = load_mapping(user)
        assert mapping["acme"].vendor == "Acme SpA"
        assert "aws" in mapping

    def test_defaults_are_not_mutated_by_a_previous_override(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("aws:\n  hq_country: US\n")
        load_mapping(user)
        assert load_mapping()["aws"].hq_country == "LU"


class TestLoaderErrors:
    def test_unknown_service_code_raises(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("aws:\n  services: [S42]\n")
        with pytest.raises(MappingError, match="S42"):
            load_mapping(user)

    def test_unknown_service_code_says_where(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("acme:\n  vendor: Acme\n  hq_country: IT\n  services: [S99]\n")
        with pytest.raises(MappingError, match="acme"):
            load_mapping(user)

    def test_missing_user_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(MappingError, match="nope.yaml"):
            load_mapping(tmp_path / "nope.yaml")

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("aws: [unclosed\n")
        with pytest.raises(MappingError):
            load_mapping(user)

    def test_a_yaml_that_is_not_a_mapping_raises(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("- aws\n- google\n")
        with pytest.raises(MappingError, match="mapping|object"):
            load_mapping(user)

    def test_unknown_key_in_an_entry_raises(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("aws:\n  hq_contry: US\n")
        with pytest.raises(MappingError, match="hq_contry"):
            load_mapping(user)

    def test_bad_country_raises(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("aws:\n  hq_country: Luxembourg\n")
        with pytest.raises(MappingError):
            load_mapping(user)

    def test_an_empty_user_file_is_harmless(self, tmp_path: Path) -> None:
        user = tmp_path / "map.yaml"
        user.write_text("")
        assert load_mapping(user)["aws"].hq_country == "LU"


class TestProviderToTpp:
    def test_builds_a_b_05_01_row_from_the_mapping(self) -> None:
        row = provider_to_tpp(discovered("aws"), load_mapping())
        assert row.TEMPLATE == "B_05.01"
        assert row.legal_name == "Amazon Web Services EMEA SARL"
        assert row.headquarters_country == "LU"
        assert row.person_type is TypeOfPerson.LEGAL_PERSON

    def test_everything_mapping_derived_is_inferred(self) -> None:
        row = provider_to_tpp(discovered("aws"), load_mapping())
        for field in ("legal_name", "headquarters_country", "person_type", "name_latin"):
            assert row.status_of(field) is FieldStatus.INFERRED, field

    def test_nothing_is_ever_filled(self) -> None:
        row = provider_to_tpp(discovered("aws"), load_mapping())
        assert FieldStatus.FILLED not in {p.status for p in row.provenance.values()}

    def test_provenance_names_the_mapping_as_its_source(self) -> None:
        row = provider_to_tpp(discovered("aws"), load_mapping())
        assert row.provenance["legal_name"].source == "mapping"

    def test_latin_name_mirrors_the_legal_name(self) -> None:
        row = provider_to_tpp(discovered("scaleway"), load_mapping())
        assert row.name_latin == row.legal_name == "Scaleway SAS"

    def test_identification_code_is_left_for_gleif(self) -> None:
        row = provider_to_tpp(discovered("aws"), load_mapping())
        assert row.identification_code is None
        assert row.status_of("identification_code") is FieldStatus.MISSING


class TestUnmappedProvider:
    def test_an_unknown_provider_still_yields_a_row(self) -> None:
        row = provider_to_tpp(discovered("weirdvendor"), load_mapping())
        assert row is not None
        assert row.TEMPLATE == "B_05.01"

    def test_the_terraform_name_is_used_as_a_weak_hint(self) -> None:
        row = provider_to_tpp(discovered("weirdvendor"), load_mapping())
        assert row.legal_name == "weirdvendor"
        assert row.status_of("legal_name") is FieldStatus.INFERRED

    def test_the_hint_says_it_is_not_a_legal_name(self) -> None:
        row = provider_to_tpp(discovered("weirdvendor"), load_mapping())
        note = row.provenance["legal_name"].note or ""
        assert "not a legal name" in note

    def test_no_country_is_invented(self) -> None:
        row = provider_to_tpp(discovered("weirdvendor"), load_mapping())
        assert row.headquarters_country is None
        assert row.status_of("headquarters_country") is FieldStatus.MISSING


class TestServicesFor:
    def test_known_provider_returns_its_suggested_codes(self) -> None:
        mapping = load_mapping()
        assert mapping["cloudflare"].services == [ICTServiceType.S11, ICTServiceType.S04]

    def test_entry_model_rejects_a_bad_code_directly(self) -> None:
        with pytest.raises(ValueError):
            ProviderMapping(vendor="x", hq_country="IT", services=["S42"])  # type: ignore[list-item]
