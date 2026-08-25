"""Closed lists. Where the reconstruction and the EBA files disagree, the EBA files win."""

import json

import pytest

from dora_roi.models.enums import (
    ContractualArrangementType,
    DataSensitiveness,
    FieldStatus,
    ICTServiceType,
    IdentifierType,
    LevelOfReliance,
    Priority,
    Substitutability,
    TypeOfPerson,
)


class TestICTServiceType:
    def test_round_trip_from_code(self) -> None:
        assert ICTServiceType("S17") is ICTServiceType.S17

    def test_description_lookup(self) -> None:
        assert ICTServiceType.S17.description == "Cloud services: IaaS"
        assert ICTServiceType("S19").description == "Cloud services: SaaS"

    def test_annex_iii_has_exactly_nineteen_contiguous_codes(self) -> None:
        assert [s.value for s in ICTServiceType] == [f"S{n:02d}" for n in range(1, 20)]

    def test_every_service_carries_a_description(self) -> None:
        assert all(s.description.strip() for s in ICTServiceType)

    def test_descriptions_are_distinct(self) -> None:
        descriptions = [s.description for s in ICTServiceType]
        assert len(set(descriptions)) == len(descriptions)

    def test_unknown_code_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ICTServiceType("S20")

    def test_behaves_as_a_string_for_serialisation(self) -> None:
        assert ICTServiceType.S17 == "S17"
        assert json.dumps({"svc": ICTServiceType.S17}) == '{"svc": "S17"}'


class TestIdentifierType:
    def test_lei_and_euid_stand_alone(self) -> None:
        assert IdentifierType.LEI.requires_country is False
        assert IdentifierType.EUID.requires_country is False
        assert IdentifierType.LEI.code() == "LEI"

    @pytest.mark.parametrize("member", ["CRN", "VAT", "PNR", "NIN"])
    def test_national_codes_are_country_prefixed(self, member: str) -> None:
        identifier = IdentifierType(member)
        assert identifier.requires_country is True
        assert identifier.code("IT") == f"IT_{member}"

    def test_country_is_required_for_national_codes(self) -> None:
        with pytest.raises(ValueError, match="country"):
            IdentifierType.VAT.code()

    def test_country_is_rejected_for_lei(self) -> None:
        with pytest.raises(ValueError, match="country"):
            IdentifierType.LEI.code("IT")

    def test_parse_round_trip(self) -> None:
        assert IdentifierType.parse("IT_VAT") == (IdentifierType.VAT, "IT")
        assert IdentifierType.parse("LEI") == (IdentifierType.LEI, None)

    def test_parse_rejects_malformed(self) -> None:
        for raw in ("IT-VAT", "ITA_VAT", "IT_XXX", "it_vat", ""):
            with pytest.raises(ValueError):
                IdentifierType.parse(raw)


class TestOtherClosedLists:
    def test_contractual_arrangement_type(self) -> None:
        assert [t.value for t in ContractualArrangementType] == [
            "Standalone arrangement",
            "Overarching arrangement",
            "Subsequent or associated arrangement",
        ]

    def test_level_of_reliance(self) -> None:
        assert [t.value for t in LevelOfReliance] == [
            "Not significant",
            "Low reliance",
            "Material reliance",
            "Full reliance",
        ]

    def test_data_sensitiveness(self) -> None:
        assert [t.value for t in DataSensitiveness] == ["Low", "Medium", "High"]

    def test_substitutability(self) -> None:
        assert [t.value for t in Substitutability] == [
            "Not substitutable",
            "Highly complex substitutability",
            "Medium complexity in terms of substitutability",
            "Easily substitutable",
        ]

    def test_type_of_person(self) -> None:
        assert [t.value for t in TypeOfPerson] == [
            "Legal person, excluding individual acting in a business capacity",
            "Individual acting in a business capacity",
        ]


class TestToolStatuses:
    def test_field_status(self) -> None:
        assert [s.value for s in FieldStatus] == ["FILLED", "INFERRED", "MISSING"]

    def test_priority(self) -> None:
        assert [p.value for p in Priority] == ["BLOCKING", "NON_BLOCKING"]

    def test_statuses_are_strings(self) -> None:
        assert FieldStatus.INFERRED == "INFERRED"
        assert Priority.BLOCKING == "BLOCKING"
