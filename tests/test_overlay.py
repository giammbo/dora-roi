"""Manual overlay: the facts only a human can assert."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dora_roi.models.enums import ContractualArrangementType, FieldStatus, TypeOfPerson
from dora_roi.models.templates import (
    ContractualArrangementGeneral,
    RegisterOfInformation,
    ThirdPartyProvider,
)
from dora_roi.overlay.vendors import (
    Overlay,
    OverlayError,
    apply_overlay,
    load_overlay,
    overlay_template,
)

LEI = "5493001KJTIIGC8Y1R12"


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "vendors.yaml"
    path.write_text(body)
    return path


def register(*names: str) -> RegisterOfInformation:
    """A register shaped like the one `scan` builds: one arrangement per provider."""
    providers, arrangements = [], []
    for name in names:
        row = ThirdPartyProvider(legal_name=name.title(), source_key=name)
        row.mark("legal_name", FieldStatus.INFERRED, source="mapping")
        providers.append(row)
        arrangements.append(
            ContractualArrangementGeneral(arrangement_reference=f"ARR-{name.upper()}-001", source_key=name)
        )
    return RegisterOfInformation(providers=providers, arrangements=arrangements)


class TestSchema:
    def test_loads_a_minimal_file(self, tmp_path: Path) -> None:
        overlay = load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: Acme SarL\n"))
        assert overlay.providers["aws"].legal_name == "Acme SarL"

    def test_an_empty_file_is_an_empty_overlay(self, tmp_path: Path) -> None:
        assert load_overlay(write(tmp_path, "")) == Overlay()

    def test_a_typo_in_a_provider_field_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError, match="legal_nmae"):
            load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_nmae: Acme\n"))

    def test_a_typo_at_the_top_level_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError, match="provider"):
            load_overlay(write(tmp_path, "provider:\n  aws:\n    legal_name: Acme\n"))

    def test_the_provider_that_owns_the_mistake_is_named(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError, match="datadog"):
            load_overlay(write(tmp_path, "providers:\n  datadog:\n    nope: 1\n"))

    def test_a_malformed_lei_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError, match="LEI|lei"):
            load_overlay(write(tmp_path, "providers:\n  aws:\n    identification_code: NOPE\n    type_of_code: LEI\n"))

    def test_a_bad_country_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError):
            load_overlay(write(tmp_path, "providers:\n  aws:\n    hq_country: Luxembourg\n"))

    def test_a_closed_list_value_outside_the_list_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError):
            load_overlay(write(tmp_path, "providers:\n  aws:\n    arrangement:\n      type: Handshake\n"))

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError, match="nope.yaml"):
            load_overlay(tmp_path / "nope.yaml")

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError, match="YAML"):
            load_overlay(write(tmp_path, "providers: [unclosed\n"))


class TestPrecedence:
    def test_overlay_wins_over_enrichment(self, tmp_path: Path) -> None:
        roi = register("aws")
        overlay = load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: Acme Cloud SpA\n"))
        apply_overlay(roi, overlay)
        assert roi.providers[0].legal_name == "Acme Cloud SpA"

    def test_provenance_flips_to_filled(self, tmp_path: Path) -> None:
        roi = register("aws")
        assert roi.providers[0].status_of("legal_name") is FieldStatus.INFERRED
        apply_overlay(roi, load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: Acme\n")))
        assert roi.providers[0].status_of("legal_name") is FieldStatus.FILLED
        assert roi.providers[0].provenance["legal_name"].source == "overlay"

    def test_untouched_fields_keep_their_inferred_status(self, tmp_path: Path) -> None:
        roi = register("aws")
        roi.providers[0].headquarters_country = "LU"
        roi.providers[0].mark("headquarters_country", FieldStatus.INFERRED, source="mapping")
        apply_overlay(roi, load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: Acme\n")))
        assert roi.providers[0].status_of("headquarters_country") is FieldStatus.INFERRED

    def test_a_provider_with_no_overlay_block_is_untouched(self, tmp_path: Path) -> None:
        roi = register("aws", "datadog")
        apply_overlay(roi, load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: Acme\n")))
        assert roi.providers[1].status_of("legal_name") is FieldStatus.INFERRED


class TestWhatTheOverlayCanSay:
    def test_the_full_provider_block(self, tmp_path: Path) -> None:
        roi = register("aws")
        overlay = load_overlay(
            write(
                tmp_path,
                f"""
providers:
  aws:
    identification_code: {LEI}
    type_of_code: LEI
    legal_name: Amazon Web Services EMEA SARL
    person_type: Legal person, excluding individual acting in a business capacity
    hq_country: LU
    currency: EUR
    total_annual_expense: 120000.50
    ultimate_parent_code: {LEI}
    type_of_parent_code: LEI
""",
            )
        )
        apply_overlay(roi, overlay)
        row = roi.providers[0]
        assert row.identification_code == LEI
        assert row.person_type is TypeOfPerson.LEGAL_PERSON
        assert row.total_annual_expense == Decimal("120000.50")
        assert all(
            row.status_of(f) is FieldStatus.FILLED
            for f in ("identification_code", "hq_country".replace("hq_", "headquarters_"))
        )

    def test_the_arrangement_block(self, tmp_path: Path) -> None:
        roi = register("aws")
        overlay = load_overlay(
            write(
                tmp_path,
                """
providers:
  aws:
    arrangement:
      reference: CTR-2024-0917
      type: Standalone arrangement
      currency: EUR
      annual_expense: 98000
""",
            )
        )
        apply_overlay(roi, overlay)
        arrangement = roi.arrangements[0]
        assert arrangement.arrangement_reference == "CTR-2024-0917"
        assert arrangement.arrangement_type is ContractualArrangementType.STANDALONE
        assert arrangement.status_of("arrangement_reference") is FieldStatus.FILLED

    def test_a_real_contract_reference_replaces_the_synthetic_one(self, tmp_path: Path) -> None:
        roi = register("aws")
        assert roi.arrangements[0].arrangement_reference == "ARR-AWS-001"
        body = "providers:\n  aws:\n    arrangement:\n      reference: CTR-1\n"
        apply_overlay(roi, load_overlay(write(tmp_path, body)))
        assert roi.arrangements[0].arrangement_reference == "CTR-1"

    def test_the_entity_block(self, tmp_path: Path) -> None:
        roi = register("aws")
        overlay = load_overlay(
            write(
                tmp_path,
                f"""
entity:
  lei: {LEI}
  name: Acme Payments SpA
  country: IT
  entity_type: Payment institution
  competent_authority: Banca d'Italia
  reporting_date: 2026-03-31
""",
            )
        )
        apply_overlay(roi, overlay)
        assert roi.entity is not None
        assert roi.entity.lei == LEI
        assert roi.entity.reporting_date == date(2026, 3, 31)
        assert roi.entity.status_of("lei") is FieldStatus.FILLED


class TestUnmatchedKeys:
    def test_an_overlay_key_matching_nothing_is_reported(self, tmp_path: Path) -> None:
        roi = register("aws")
        overlay = load_overlay(write(tmp_path, "providers:\n  awz:\n    legal_name: Typo Ltd\n"))
        warnings = apply_overlay(roi, overlay)
        assert any("awz" in w for w in warnings)

    def test_a_matched_key_produces_no_warning(self, tmp_path: Path) -> None:
        roi = register("aws")
        overlay = load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: Acme\n"))
        assert apply_overlay(roi, overlay) == []


class TestContractDetail:
    """A5 gave B_02.02 a row model, so the `contract:` block now lands in one."""

    def test_contract_detail_is_accepted(self, tmp_path: Path) -> None:
        overlay = load_overlay(
            write(
                tmp_path,
                """
providers:
  aws:
    contract:
      start_date: 2024-01-01
      notice_period_entity_days: 90
      governing_law_country: IT
""",
            )
        )
        assert overlay.providers["aws"].contract is not None
        assert overlay.providers["aws"].contract.notice_period_entity_days == 90

    def test_it_becomes_a_b_02_02_row(self, tmp_path: Path) -> None:
        roi = register("aws")
        body = "providers:\n  aws:\n    contract:\n      governing_law_country: IT\n"
        assert apply_overlay(roi, load_overlay(write(tmp_path, body))) == []
        assert len(roi.arrangement_details) == 1
        assert roi.arrangement_details[0].governing_law_country == "IT"
        assert roi.arrangement_details[0].status_of("governing_law_country") is FieldStatus.FILLED

    def test_the_row_joins_back_to_its_arrangement(self, tmp_path: Path) -> None:
        roi = register("aws")
        body = "providers:\n  aws:\n    contract:\n      notice_period_entity_days: 90\n"
        apply_overlay(roi, load_overlay(write(tmp_path, body)))
        assert roi.arrangement_details[0].arrangement_reference == "ARR-AWS-001"
        assert roi.arrangement_details[0].notice_period_entity == 90

    def test_no_contract_block_means_no_row(self, tmp_path: Path) -> None:
        roi = register("aws")
        apply_overlay(roi, load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: A\n")))
        assert roi.arrangement_details == []

    def test_dates_that_run_backwards_are_refused(self, tmp_path: Path) -> None:
        roi = register("aws")
        body = "providers:\n  aws:\n    contract:\n      start_date: 2025-01-01\n      end_date: 2024-01-01\n"
        with pytest.raises(OverlayError, match="precedes"):
            apply_overlay(roi, load_overlay(write(tmp_path, body)))

    def test_a_typo_inside_the_parked_block_is_still_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError, match="governing_law_contry"):
            load_overlay(write(tmp_path, "providers:\n  aws:\n    contract:\n      governing_law_contry: IT\n"))


class TestTemplate:
    def test_seeds_a_block_per_discovered_provider(self) -> None:
        text = overlay_template(["aws", "datadog"])
        assert "  aws:" in text and "  datadog:" in text

    def test_is_valid_yaml_and_loads_back(self, tmp_path: Path) -> None:
        path = write(tmp_path, overlay_template(["aws"]))
        assert load_overlay(path) is not None

    def test_is_commented_so_nothing_is_asserted_by_accident(self, tmp_path: Path) -> None:
        overlay = load_overlay(write(tmp_path, overlay_template(["aws"])))
        assert overlay.providers.get("aws") is None or overlay.providers["aws"].legal_name is None

    def test_explains_that_overlay_values_become_filled(self) -> None:
        assert "FILLED" in overlay_template(["aws"])

    def test_with_no_providers_still_produces_a_usable_file(self, tmp_path: Path) -> None:
        assert load_overlay(write(tmp_path, overlay_template([]))) is not None


class TestMatchingIsByKeyNotPosition:
    """The whole point of source_key: order stops being load-bearing."""

    def test_a_reordered_register_still_matches_correctly(self, tmp_path: Path) -> None:
        roi = register("aws", "datadog")
        roi.providers.reverse()
        roi.arrangements.reverse()
        overlay = load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: The Real AWS Entity\n"))
        assert apply_overlay(roi, overlay) == []
        by_key = {row.source_key: row for row in roi.providers}
        assert by_key["aws"].legal_name == "The Real AWS Entity"
        assert by_key["datadog"].legal_name == "Datadog"

    def test_the_arrangement_follows_its_own_provider(self, tmp_path: Path) -> None:
        roi = register("aws", "datadog")
        roi.arrangements.reverse()
        overlay = load_overlay(write(tmp_path, "providers:\n  aws:\n    arrangement:\n      reference: CTR-AWS\n"))
        apply_overlay(roi, overlay)
        by_key = {row.source_key: row for row in roi.arrangements}
        assert by_key["aws"].arrangement_reference == "CTR-AWS"
        assert by_key["datadog"].arrangement_reference == "ARR-DATADOG-001"

    def test_a_row_with_no_source_key_is_simply_not_matched(self, tmp_path: Path) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="Orphan")])
        overlay = load_overlay(write(tmp_path, "providers:\n  aws:\n    legal_name: X\n"))
        assert any("aws" in w for w in apply_overlay(roi, overlay))
        assert roi.providers[0].legal_name == "Orphan"

    def test_source_key_never_reaches_a_filing(self) -> None:
        row = ThirdPartyProvider(legal_name="Acme", source_key="aws")
        assert "source_key" not in row.model_dump(by_alias=True)
        assert "source_key" not in row.model_dump()


class TestTheTemplateSaysWhatIsAlreadySettled:
    """Ten identical blocks tell you nothing about where to spend an afternoon."""

    def test_a_settled_field_is_shown_as_confirmed(self) -> None:
        text = overlay_template(["google"], {"google": {"identification_code": "98450052CF14CFEB6435"}})
        assert "already confirmed: 98450052CF14CFEB6435" in text

    def test_a_missing_identification_code_is_flagged_as_blocking(self) -> None:
        text = overlay_template(["aws"], {"aws": {}})
        assert "no identification code was resolved" in text
        assert "blocking gap" in text

    def test_it_says_what_to_do_when_there_is_no_lei(self) -> None:
        """AWS EMEA SARL has none, so "look it up on GLEIF" is not enough advice."""
        assert "national code" in overlay_template(["aws"], {"aws": {}})

    def test_a_settled_provider_is_not_nagged(self) -> None:
        text = overlay_template(["google"], {"google": {"identification_code": "X", "legal_name": "Y"}})
        assert "no identification code was resolved" not in text

    def test_two_providers_get_different_blocks(self) -> None:
        text = overlay_template(["google", "aws"], {"google": {"identification_code": "X"}, "aws": {}})
        google = text[text.index("  google:") : text.index("  aws:")]
        aws = text[text.index("  aws:") :]
        assert "already confirmed" in google and "already confirmed" not in aws
        assert "blocking gap" in aws and "blocking gap" not in google

    def test_it_still_loads_back_with_nothing_asserted(self, tmp_path: Path) -> None:
        body = overlay_template(["aws", "google"], {"google": {"identification_code": "X"}})
        overlay = load_overlay(write(tmp_path, body))
        assert all(entry.identification_code is None for entry in overlay.providers.values())

    def test_no_stale_promise_about_work_that_shipped(self) -> None:
        """The generated file used to say B_02.02 would be "applied in A5". It shipped."""
        assert "A5" not in overlay_template(["aws"], {})
