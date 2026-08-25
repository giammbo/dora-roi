"""Register rows, provenance and the field catalog.

Counts and mandatory flags began as a reconstruction from the ITS; golden rule 6
means the official EBA annotated template wins over it.
"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from dora_roi.models.enums import ContractualArrangementType, FieldStatus, ICTServiceType, TypeOfPerson
from dora_roi.models.templates import (
    FIELD_CATALOG,
    MODELLED_TEMPLATES,
    TEMPLATE_COLLECTIONS,
    ContractualArrangementGeneral,
    EntityMaintainingRegister,
    Provenance,
    RegisterOfInformation,
    SupplyChainLink,
    ThirdPartyProvider,
)

VALID_LEI = "529900T8BM49AURSDO55"
OTHER_LEI = "213800WAVVOPS85N2205"

# Reconciled against the official EBA annotated layout. Where these differed
# from the drafting notes, the EBA file won — that is golden rule 6, and it
# moved five of them.
EXPECTED_FIELD_COUNTS = {
    "B_01.01": 6,
    "B_01.02": 11,
    "B_01.03": 4,
    "B_02.01": 5,
    "B_02.02": 18,
    "B_02.03": 3,  # the notes said 2: the official layout has a `Link` column
    "B_03.01": 3,  # the notes said 2: same
    "B_03.02": 3,
    "B_03.03": 3,  # the notes said 2: same, and its link column is 0031
    "B_04.01": 4,
    "B_05.01": 12,
    "B_05.02": 7,
    "B_06.01": 10,
    "B_07.01": 12,
    "B_99.01": 19,  # the notes said 3 and described a different template entirely
}


class TestFieldCatalog:
    def test_covers_all_fifteen_templates(self) -> None:
        assert set(FIELD_CATALOG) == set(EXPECTED_FIELD_COUNTS)
        assert len(FIELD_CATALOG) == 15

    def test_b_02_02_has_eighteen_fields(self) -> None:
        assert len(FIELD_CATALOG["B_02.02"]) == 18

    def test_every_template_matches_the_documented_field_count(self) -> None:
        actual = {template: len(fields) for template, fields in FIELD_CATALOG.items()}
        assert actual == EXPECTED_FIELD_COUNTS

    def test_codes_are_unique_and_four_digits(self) -> None:
        for template, fields in FIELD_CATALOG.items():
            codes = [f.code for f in fields]
            assert len(set(codes)) == len(codes), f"duplicate code in {template}"
            assert all(c.isdigit() and len(c) == 4 for c in codes), f"malformed code in {template}"

    def test_every_field_has_a_name(self) -> None:
        for template, fields in FIELD_CATALOG.items():
            assert all(f.name.strip() for f in fields), f"unnamed field in {template}"

    def test_conditional_mandatory_carries_its_condition(self) -> None:
        # B_02.02.0150 is mandatory only when data is stored.
        location = next(f for f in FIELD_CATALOG["B_02.02"] if f.code == "0150")
        assert location.mandatory is True
        assert location.condition is not None
        # Unconditional fields must not carry one.
        provision = next(f for f in FIELD_CATALOG["B_02.02"] if f.code == "0130")
        assert provision.condition is None

    def test_b_04_01_0040_is_the_only_optional_link_table_field(self) -> None:
        link_tables = ("B_02.03", "B_03.01", "B_03.02", "B_03.03", "B_04.01")
        optional = [(t, f.code) for t in link_tables for f in FIELD_CATALOG[t] if not f.mandatory]
        assert optional == [("B_04.01", "0040")]


class TestCatalogModelParity:
    """Every catalog code of every template must map to a model field."""

    def test_all_fifteen_templates_are_modelled(self) -> None:
        assert set(MODELLED_TEMPLATES) == set(FIELD_CATALOG)

    @pytest.mark.parametrize("template", sorted(EXPECTED_FIELD_COUNTS))
    def test_codes_and_aliases_agree(self, template: str) -> None:
        model = MODELLED_TEMPLATES[template]
        aliases = {f.alias for f in model.model_fields.values() if f.alias is not None}
        assert aliases == {f.code for f in FIELD_CATALOG[template]}

    @pytest.mark.parametrize("template", sorted(EXPECTED_FIELD_COUNTS))
    def test_the_model_declares_its_own_template_code(self, template: str) -> None:
        assert MODELLED_TEMPLATES[template].TEMPLATE == template

    def test_every_template_has_a_collection_on_the_register(self) -> None:
        assert set(TEMPLATE_COLLECTIONS) == set(FIELD_CATALOG)
        roi = RegisterOfInformation()
        for attribute in TEMPLATE_COLLECTIONS.values():
            assert hasattr(roi, attribute)


class TestProvenance:
    def test_defaults_to_missing_for_an_unmarked_field(self) -> None:
        row = ThirdPartyProvider()
        assert row.status_of("legal_name") is FieldStatus.MISSING

    def test_mark_records_status_source_and_note(self) -> None:
        row = ThirdPartyProvider(legal_name="Amazon Web Services EMEA SARL")
        row.mark("legal_name", FieldStatus.INFERRED, source="mapping", note="vendor hint")
        assert row.status_of("legal_name") is FieldStatus.INFERRED
        assert row.provenance["legal_name"] == Provenance(
            status=FieldStatus.INFERRED, source="mapping", note="vendor hint"
        )

    def test_mark_is_chainable(self) -> None:
        row = (
            ThirdPartyProvider()
            .mark("legal_name", FieldStatus.INFERRED)
            .mark("headquarters_country", FieldStatus.INFERRED)
        )
        assert row.status_of("headquarters_country") is FieldStatus.INFERRED

    def test_mark_rejects_a_field_the_row_does_not_have(self) -> None:
        with pytest.raises(ValueError, match="no field"):
            ThirdPartyProvider().mark("legal_nmae", FieldStatus.FILLED)

    def test_provenance_never_reaches_a_filing(self) -> None:
        row = ThirdPartyProvider(legal_name="Datadog").mark("legal_name", FieldStatus.INFERRED)
        assert "provenance" not in row.model_dump(by_alias=True)
        assert "provenance" not in row.model_dump()


class TestOfficialCodeAliases:
    def test_dump_by_alias_emits_official_codes(self) -> None:
        row = EntityMaintainingRegister(lei=VALID_LEI, name="Acme SpA", country="IT")
        dumped = row.model_dump(by_alias=True)
        assert dumped["0010"] == VALID_LEI
        assert dumped["0020"] == "Acme SpA"
        assert dumped["0030"] == "IT"
        assert set(dumped) == {f.code for f in FIELD_CATALOG["B_01.01"]}

    def test_rows_can_be_built_from_official_codes(self) -> None:
        row = EntityMaintainingRegister(**{"0010": VALID_LEI, "0020": "Acme SpA"})
        assert row.lei == VALID_LEI

    def test_dump_without_alias_keeps_python_names(self) -> None:
        row = EntityMaintainingRegister(name="Acme SpA")
        assert row.model_dump()["name"] == "Acme SpA"


class TestValidators:
    @pytest.mark.parametrize(
        "bad",
        [
            "529900T8BM49AURSDO5",  # 19 chars
            "529900T8BM49AURSDO555",  # 21 chars
            "529900T8BM49AURSDOAA",  # check digits not numeric
            "529900t8bm49aursdo55",  # lower case
            "529900-T8BM49AURSDO5",  # punctuation
            "",
        ],
    )
    def test_invalid_lei_raises(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            EntityMaintainingRegister(lei=bad)

    def test_valid_lei_is_accepted(self) -> None:
        assert EntityMaintainingRegister(lei=VALID_LEI).lei == VALID_LEI

    @pytest.mark.parametrize("bad", ["ITA", "i", "it", "1T", ""])
    def test_invalid_country_raises(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            EntityMaintainingRegister(country=bad)

    @pytest.mark.parametrize("bad", ["EURO", "eu", "EU", "12"])
    def test_invalid_currency_raises(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            ContractualArrangementGeneral(currency=bad)

    def test_currency_accepts_iso4217(self) -> None:
        assert ContractualArrangementGeneral(currency="EUR").currency == "EUR"

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EntityMaintainingRegister(lei=VALID_LEI, nmae="typo")

    def test_supply_chain_rank_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            SupplyChainLink(rank=0)
        assert SupplyChainLink(rank=1).rank == 1


class TestAssignmentIsValidatedToo:
    """The pipeline fills rows field by field, so assignment is the real edge."""

    def test_a_bad_lei_cannot_be_assigned(self) -> None:
        row = EntityMaintainingRegister()
        with pytest.raises(ValidationError):
            row.lei = "NOT-A-LEI"

    def test_a_good_lei_can(self) -> None:
        row = EntityMaintainingRegister()
        row.lei = VALID_LEI
        assert row.lei == VALID_LEI

    def test_the_cross_field_rule_holds_on_assignment(self) -> None:
        row = ThirdPartyProvider(identification_code="whatever")
        with pytest.raises(ValidationError, match="LEI"):
            row.type_of_code = "LEI"

    def test_marking_provenance_does_not_trip_validation(self) -> None:
        row = ThirdPartyProvider(identification_code=VALID_LEI)
        row.type_of_code = "LEI"
        row.mark("identification_code", FieldStatus.FILLED, source="gleif")
        assert row.status_of("identification_code") is FieldStatus.FILLED


class TestIdentificationCodeConsistency:
    """Bad LEIs are a third of first-filing failures."""

    def test_lei_typed_code_must_be_a_valid_lei(self) -> None:
        with pytest.raises(ValidationError, match="LEI"):
            ThirdPartyProvider(identification_code="NOT-A-LEI", type_of_code="LEI")

    def test_lei_typed_code_accepts_a_valid_lei(self) -> None:
        row = ThirdPartyProvider(identification_code=VALID_LEI, type_of_code="LEI")
        assert row.identification_code == VALID_LEI

    def test_national_code_is_not_held_to_the_lei_shape(self) -> None:
        row = ThirdPartyProvider(identification_code="IT12345678901", type_of_code="IT_VAT")
        assert row.identification_code == "IT12345678901"

    def test_the_same_rule_applies_to_the_ultimate_parent(self) -> None:
        with pytest.raises(ValidationError, match="LEI"):
            ThirdPartyProvider(ultimate_parent_code="NOPE", type_of_parent_code="LEI")
        assert ThirdPartyProvider(ultimate_parent_code=OTHER_LEI, type_of_parent_code="LEI")


class TestRowsAcceptClosedListValues:
    def test_arrangement_type(self) -> None:
        row = ContractualArrangementGeneral(arrangement_type=ContractualArrangementType.STANDALONE)
        assert row.model_dump(by_alias=True)["0020"] == "Standalone arrangement"

    def test_service_type(self) -> None:
        row = SupplyChainLink(service_type=ICTServiceType.S17)
        assert row.model_dump(by_alias=True)["0020"] == "S17"

    def test_person_type(self) -> None:
        row = ThirdPartyProvider(person_type=TypeOfPerson.LEGAL_PERSON)
        assert (
            row.model_dump(by_alias=True)["0070"] == "Legal person, excluding individual acting in a business capacity"
        )

    def test_closed_list_values_are_enforced(self) -> None:
        with pytest.raises(ValidationError):
            ContractualArrangementGeneral(arrangement_type="Whatever we agreed")


class TestPartialRowsAreLegal:
    """A prefill has holes by construction: the gap report finds them, not the model."""

    def test_every_row_type_builds_empty(self) -> None:
        for model in MODELLED_TEMPLATES.values():
            assert model() is not None

    def test_mandatory_fields_are_not_enforced_at_construction(self) -> None:
        # 0010/0020/0030 are all mandatory in the catalog, none is required here.
        assert EntityMaintainingRegister().lei is None


class TestRegisterOfInformation:
    def test_starts_empty(self) -> None:
        roi = RegisterOfInformation()
        assert roi.entity is None
        assert roi.arrangements == []
        assert roi.providers == []
        assert roi.supply_chain == []

    def test_collections_are_not_shared_between_instances(self) -> None:
        first = RegisterOfInformation()
        first.providers.append(ThirdPartyProvider(legal_name="Datadog"))
        assert RegisterOfInformation().providers == []

    def test_holds_a_populated_register(self) -> None:
        roi = RegisterOfInformation(
            entity=EntityMaintainingRegister(lei=VALID_LEI, reporting_date=date(2026, 3, 31)),
            arrangements=[ContractualArrangementGeneral(arrangement_reference="ARR-AWS-001")],
            providers=[ThirdPartyProvider(legal_name="AWS", total_annual_expense=Decimal("1234.56"))],
            supply_chain=[SupplyChainLink(arrangement_reference="ARR-AWS-001", rank=1)],
        )
        assert roi.entity is not None
        assert roi.providers[0].total_annual_expense == Decimal("1234.56")
        assert roi.supply_chain[0].rank == 1
