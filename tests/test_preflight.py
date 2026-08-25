"""Pre-flight validation: the checks that stand between a prefill and a rejection."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from dora_roi.export.preflight import (
    Finding,
    PreflightError,
    Severity,
    load_prefill,
    preflight,
    summarise_findings,
)
from dora_roi.models.enums import FieldStatus, ICTServiceType
from dora_roi.models.templates import (
    ContractualArrangementGeneral,
    EntityMaintainingRegister,
    RegisterOfInformation,
    SupplyChainLink,
    ThirdPartyProvider,
)

LEI = "5493001KJTIIGC8Y1R12"
OTHER_LEI = "529900T8BM49AURSDO55"


def codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


def complete_provider(name: str = "Amazon Web Services EMEA SARL") -> ThirdPartyProvider:
    row = ThirdPartyProvider(
        identification_code=LEI,
        type_of_code="LEI",
        legal_name=name,
        person_type="Legal person, excluding individual acting in a business capacity",
        headquarters_country="LU",
        ultimate_parent_code=LEI,
    )
    for field in row.roi_fields():
        if getattr(row, field) is not None:
            row.mark(field, FieldStatus.FILLED, source="overlay")
    return row


class TestMandatoryCompleteness:
    def test_a_missing_mandatory_field_is_blocking(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider()])
        found = [f for f in preflight(roi) if f.code == "MANDATORY_MISSING"]
        assert found
        assert all(f.severity is Severity.BLOCKING for f in found)

    def test_it_names_the_template_the_code_and_the_row(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="Acme")])
        finding = next(f for f in preflight(roi) if f.code == "MANDATORY_MISSING")
        assert finding.template.startswith("B_")
        assert finding.field
        assert finding.row_key

    def test_a_complete_provider_raises_none_for_itself(self) -> None:
        roi = RegisterOfInformation(providers=[complete_provider()])
        provider_findings = [f for f in preflight(roi) if f.template == "B_05.01" and f.code == "MANDATORY_MISSING"]
        assert provider_findings == []

    def test_an_inferred_value_is_not_treated_as_complete(self) -> None:
        row = ThirdPartyProvider(legal_name="Guessed Ltd")
        row.mark("legal_name", FieldStatus.INFERRED, source="mapping")
        findings = preflight(RegisterOfInformation(providers=[row]))
        assert "UNREVIEWED_INFERRED" in codes(findings)

    def test_inferred_is_a_warning_not_a_block(self) -> None:
        row = ThirdPartyProvider(legal_name="Guessed Ltd")
        row.mark("legal_name", FieldStatus.INFERRED, source="mapping")
        finding = next(f for f in preflight(RegisterOfInformation(providers=[row])) if f.code == "UNREVIEWED_INFERRED")
        assert finding.severity is Severity.WARNING


class TestLeiChecks:
    def test_a_provider_with_no_identification_code_is_blocking(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="Acme")])
        assert "LEI_MISSING" in codes(preflight(roi))

    def test_a_well_formed_lei_passes_the_format_check(self) -> None:
        roi = RegisterOfInformation(providers=[complete_provider()])
        assert "LEI_MALFORMED" not in codes(preflight(roi))

    def test_a_malformed_lei_never_survives_construction(self) -> None:
        """The models refuse it, so preflight is defence in depth, not the only gate."""
        with pytest.raises(ValidationError):
            ThirdPartyProvider(identification_code="NOPE", type_of_code="LEI")

    def test_a_hand_edited_prefill_is_refused_when_it_is_loaded(self, tmp_path: Path) -> None:
        path = tmp_path / "roi_prefill.json"
        path.write_text(json.dumps({"templates": {"B_05.01": [{"values": {"0010": "NOPE", "0020": "LEI"}}]}}))
        with pytest.raises(PreflightError, match="B_05.01"):
            load_prefill(path)

    def test_preflight_still_catches_one_that_reached_it_unvalidated(self) -> None:
        row = ThirdPartyProvider.model_construct(identification_code="NOPE", type_of_code="LEI", legal_name="Acme")
        roi = RegisterOfInformation.model_construct(providers=[row], arrangements=[], supply_chain=[])
        assert "LEI_MALFORMED" in codes(preflight(roi))

    def test_gleif_status_is_checked_when_a_client_is_given(self) -> None:
        class Gleif:
            def lookup_by_lei(self, lei: str):
                from dora_roi.enrichment.gleif import LeiRecord, MatchType

                return LeiRecord(lei=lei, legal_name="X", country="LU", status="LAPSED", match_type=MatchType.EXACT)

        findings = preflight(RegisterOfInformation(providers=[complete_provider()]), gleif=Gleif())
        assert "LEI_NOT_ACTIVE" in codes(findings)

    def test_an_lei_gleif_does_not_know_is_blocking(self) -> None:
        class Gleif:
            def lookup_by_lei(self, lei: str):
                return None

        findings = preflight(RegisterOfInformation(providers=[complete_provider()]), gleif=Gleif())
        assert "LEI_UNKNOWN_TO_GLEIF" in codes(findings)

    def test_without_a_client_gleif_is_not_consulted(self) -> None:
        findings = preflight(RegisterOfInformation(providers=[complete_provider()]))
        assert "LEI_NOT_ACTIVE" not in codes(findings)
        assert "LEI_UNKNOWN_TO_GLEIF" not in codes(findings)


def row_register(row: ThirdPartyProvider) -> RegisterOfInformation:
    return RegisterOfInformation(providers=[row])


class TestReportingDate:
    def test_a_register_with_no_reporting_date_is_blocking(self) -> None:
        assert "REPORTING_DATE_MISSING" in codes(preflight(RegisterOfInformation()))

    def test_one_date_satisfies_it(self) -> None:
        roi = RegisterOfInformation(entity=EntityMaintainingRegister(reporting_date=date(2026, 3, 31)))
        assert "REPORTING_DATE_MISSING" not in codes(preflight(roi))


class TestSupplyChain:
    def test_a_hyperscaler_with_no_subcontractor_is_flagged(self) -> None:
        roi = RegisterOfInformation(
            providers=[complete_provider("Amazon Web Services EMEA SARL")],
            supply_chain=[SupplyChainLink(arrangement_reference="A", service_type=ICTServiceType.S17, rank=1)],
        )
        assert "SUPPLY_CHAIN_RANK1_ONLY" in codes(preflight(roi))

    def test_the_finding_says_not_to_omit_it_silently(self) -> None:
        roi = RegisterOfInformation(
            providers=[complete_provider()],
            supply_chain=[SupplyChainLink(arrangement_reference="A", service_type=ICTServiceType.S17, rank=1)],
        )
        finding = next(f for f in preflight(roi) if f.code == "SUPPLY_CHAIN_RANK1_ONLY")
        assert finding.fix

    def test_a_rank_two_link_clears_it(self) -> None:
        roi = RegisterOfInformation(
            providers=[complete_provider()],
            supply_chain=[
                SupplyChainLink(arrangement_reference="A", service_type=ICTServiceType.S17, rank=1),
                SupplyChainLink(arrangement_reference="A", service_type=ICTServiceType.S17, rank=2),
            ],
        )
        assert "SUPPLY_CHAIN_RANK1_ONLY" not in codes(preflight(roi))

    def test_a_non_hyperscaler_is_not_nagged(self) -> None:
        roi = RegisterOfInformation(
            providers=[complete_provider("Some Small Vendor BV")],
            supply_chain=[SupplyChainLink(arrangement_reference="A", rank=1)],
        )
        assert "SUPPLY_CHAIN_RANK1_ONLY" not in codes(preflight(roi))


class TestDuplicateArrangements:
    def test_a_reference_used_twice_is_flagged(self) -> None:
        roi = RegisterOfInformation(
            arrangements=[
                ContractualArrangementGeneral(arrangement_reference="ARR-1"),
                ContractualArrangementGeneral(arrangement_reference="ARR-1"),
            ]
        )
        assert "ARRANGEMENT_REFERENCE_DUPLICATE" in codes(preflight(roi))

    def test_distinct_references_are_fine(self) -> None:
        roi = RegisterOfInformation(
            arrangements=[
                ContractualArrangementGeneral(arrangement_reference="ARR-1"),
                ContractualArrangementGeneral(arrangement_reference="ARR-2"),
            ]
        )
        assert "ARRANGEMENT_REFERENCE_DUPLICATE" not in codes(preflight(roi))


class TestSyntheticReferences:
    def test_a_generated_reference_must_be_replaced_before_filing(self) -> None:
        roi = RegisterOfInformation(arrangements=[ContractualArrangementGeneral(arrangement_reference="ARR-AWS-001")])
        assert "SYNTHETIC_REFERENCE" in codes(preflight(roi))

    def test_a_real_reference_is_accepted(self) -> None:
        roi = RegisterOfInformation(arrangements=[ContractualArrangementGeneral(arrangement_reference="CTR-2024-17")])
        assert "SYNTHETIC_REFERENCE" not in codes(preflight(roi))


class TestUnverifiedDomainData:
    def test_the_register_warns_that_some_field_names_are_unverified(self) -> None:
        assert "DOMAIN_DATA_UNVERIFIED" in codes(preflight(RegisterOfInformation()))

    def test_it_is_a_warning_about_this_tool_not_about_the_user(self) -> None:
        finding = next(f for f in preflight(RegisterOfInformation()) if f.code == "DOMAIN_DATA_UNVERIFIED")
        assert finding.severity is Severity.WARNING


class TestOrderingAndSummary:
    def test_blocking_findings_come_first(self) -> None:
        findings = preflight(RegisterOfInformation(providers=[ThirdPartyProvider()]))
        ranks = [0 if f.severity is Severity.BLOCKING else 1 for f in findings]
        assert ranks == sorted(ranks)

    def test_summary_counts_by_severity(self) -> None:
        summary = summarise_findings(preflight(RegisterOfInformation(providers=[ThirdPartyProvider()])))
        assert summary["blocking"] > 0
        assert summary["total"] == summary["blocking"] + summary["warning"]

    def test_a_clean_register_has_no_blocking_findings(self) -> None:
        roi = RegisterOfInformation(
            entity=EntityMaintainingRegister(
                lei=OTHER_LEI,
                name="Acme",
                country="IT",
                entity_type="Payment institution",
                competent_authority="Banca d'Italia",
                reporting_date=date(2026, 3, 31),
            )
        )
        for field in roi.entity.roi_fields():
            roi.entity.mark(field, FieldStatus.FILLED, source="overlay")
        blocking = [f for f in preflight(roi) if f.severity is Severity.BLOCKING and f.template == "B_01.01"]
        assert blocking == []


class TestLoadPrefill:
    def test_round_trips_a_scan_output(self, tmp_path: Path) -> None:
        payload = {
            "templates": {
                "B_05.01": [
                    {
                        "values": {"0010": LEI, "0020": "LEI", "0050": "Acme SarL"},
                        "provenance": {"0050": {"status": "INFERRED", "source": "mapping", "note": None}},
                    }
                ]
            }
        }
        path = tmp_path / "roi_prefill.json"
        path.write_text(json.dumps(payload))
        roi = load_prefill(path)
        assert roi.providers[0].legal_name == "Acme SarL"
        assert roi.providers[0].status_of("legal_name") is FieldStatus.INFERRED

    def test_an_unknown_template_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "roi_prefill.json"
        path.write_text(json.dumps({"templates": {"B_42.42": []}}))
        with pytest.raises(PreflightError, match="B_42.42"):
            load_prefill(path)

    def test_a_value_that_fails_validation_is_reported_with_its_template(self, tmp_path: Path) -> None:
        path = tmp_path / "roi_prefill.json"
        path.write_text(json.dumps({"templates": {"B_05.01": [{"values": {"0080": "Luxembourg"}}]}}))
        with pytest.raises(PreflightError, match="B_05.01"):
            load_prefill(path)

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(PreflightError, match="nope.json"):
            load_prefill(tmp_path / "nope.json")


class TestEbaValidationRules:
    """Columns an ACTIVE EBA rule requires, kept separate from our mandatory flags."""

    def test_an_empty_required_column_is_reported(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="Acme")])
        findings = [f for f in preflight(roi) if f.code == "EBA_RULE_NULL"]
        assert findings

    def test_it_is_a_warning_at_the_ebas_own_severity(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="Acme")])
        finding = next(f for f in preflight(roi) if f.code == "EBA_RULE_NULL")
        assert finding.severity is Severity.WARNING
        assert "warning" in finding.fix

    def test_it_names_the_rule_as_the_authority_not_us(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="Acme")])
        finding = next(f for f in preflight(roi) if f.code == "EBA_RULE_NULL")
        assert "EBA validation rule" in finding.message

    def test_b_05_01_0060_is_required_by_rule_though_the_notes_called_it_optional(self) -> None:
        """Name in Latin alphabet: optional in the drafting notes, required by an active rule."""
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="Acme")])
        codes = {f.field for f in preflight(roi) if f.code == "EBA_RULE_NULL" and f.template == "B_05.01"}
        assert "0060" in codes

    def test_filling_it_clears_the_finding(self) -> None:
        row = ThirdPartyProvider(legal_name="Acme", name_latin="Acme")
        row.mark("name_latin", FieldStatus.FILLED, source="overlay")
        codes = {
            f.field
            for f in preflight(RegisterOfInformation(providers=[row]))
            if f.code == "EBA_RULE_NULL" and f.template == "B_05.01"
        }
        assert "0060" not in codes

    def test_deactivated_rules_are_not_enforced(self) -> None:
        """Thirteen of the seventy-one DORA rules are withdrawn; none may appear."""
        from dora_roi.export.preflight import _eba_required

        # The link tables carry no active not-isnull rule at all.
        assert not _eba_required().get("B_03.01")
        assert not _eba_required().get("B_05.02")

    def test_the_required_set_covers_the_templates_that_have_rules(self) -> None:
        from dora_roi.export.preflight import _eba_required

        assert set(_eba_required()) == {
            "B_01.01",
            "B_01.02",
            "B_01.03",
            "B_02.01",
            "B_02.02",
            "B_04.01",
            "B_05.01",
            "B_06.01",
            "B_07.01",
        }


class TestDuplicateIdentificationCodes:
    """An LEI names one legal entity. Three vendors sharing one is a bug, not a filing."""

    def three_sharing(self) -> RegisterOfInformation:
        return RegisterOfInformation(
            providers=[
                ThirdPartyProvider(legal_name=n, identification_code=LEI, type_of_code="LEI")
                for n in ("Auth0, Inc.", "Netlify, Inc.", "Webflow, Inc.")
            ]
        )

    def test_it_is_blocking(self) -> None:
        finding = next(f for f in preflight(self.three_sharing()) if f.code == "IDENTIFICATION_CODE_REUSED")
        assert finding.severity is Severity.BLOCKING

    def test_it_names_all_of_them(self) -> None:
        finding = next(f for f in preflight(self.three_sharing()) if f.code == "IDENTIFICATION_CODE_REUSED")
        for name in ("Auth0", "Netlify", "Webflow"):
            assert name in finding.message

    def test_distinct_codes_are_fine(self) -> None:
        roi = RegisterOfInformation(
            providers=[
                ThirdPartyProvider(legal_name="A", identification_code=LEI, type_of_code="LEI"),
                ThirdPartyProvider(legal_name="B", identification_code=OTHER_LEI, type_of_code="LEI"),
            ]
        )
        assert "IDENTIFICATION_CODE_REUSED" not in codes(preflight(roi))

    def test_providers_with_no_code_are_not_duplicates_of_each_other(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="A"), ThirdPartyProvider(legal_name="B")])
        assert "IDENTIFICATION_CODE_REUSED" not in codes(preflight(roi))


class TestTheSyntheticReferenceExplainsItself:
    """Most cloud contracts are click-through and have no number to look up."""

    def register_with_generated_ref(self) -> RegisterOfInformation:
        return RegisterOfInformation(arrangements=[ContractualArrangementGeneral(arrangement_reference="ARR-AWS-001")])

    def finding(self) -> Finding:
        return next(f for f in preflight(self.register_with_generated_ref()) if f.code == "SYNTHETIC_REFERENCE")

    def test_it_does_not_send_you_hunting_for_a_contract_number(self) -> None:
        assert "no vendor-issued number" in self.finding().fix

    def test_it_says_the_reference_is_yours_to_assign(self) -> None:
        assert "you* assign" in self.finding().fix.lower()

    def test_it_suggests_something_stable_to_use(self) -> None:
        fix = self.finding().fix.lower()
        assert "account id" in fix or "procurement" in fix

    def test_it_says_why_the_generated_one_is_refused(self) -> None:
        """Not because inventing is wrong — because ours moves when a provider is renamed."""
        assert "derived from the Terraform provider name" in self.finding().fix
