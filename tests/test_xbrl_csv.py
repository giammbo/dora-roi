"""xBRL-CSV exporter, against the official EBA sample package."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dora_roi.export.xbrl_csv import (
    DORA_MODULE_URL,
    ExportError,
    PackageName,
    build_package,
    write_package,
)
from dora_roi.models.enums import ContractualArrangementType, ICTServiceType, TypeOfPerson
from dora_roi.models.templates import (
    FIELD_CATALOG,
    ContractualArrangementGeneral,
    EntityMaintainingRegister,
    RegisterOfInformation,
    SupplyChainLink,
    ThirdPartyProvider,
)

SAMPLE = Path(__file__).parent / "fixtures" / "eba" / "sample_dora_package.zip"
LEI = "5493001KJTIIGC8Y1R12"


def name() -> PackageName:
    return PackageName(
        lei=LEI,
        consolidated=True,
        country="IT",
        reference_date=date(2026, 3, 31),
        timestamp="20260331120000000",
    )


def register() -> RegisterOfInformation:
    return RegisterOfInformation(
        entity=EntityMaintainingRegister(
            lei=LEI, name="Acme Payments SpA", country="IT", reporting_date=date(2026, 3, 31)
        ),
        arrangements=[
            ContractualArrangementGeneral(
                arrangement_reference="CTR-1",
                arrangement_type=ContractualArrangementType.STANDALONE,
                currency="EUR",
                annual_expense=Decimal("120000.50"),
            )
        ],
        providers=[
            ThirdPartyProvider(
                identification_code=LEI,
                type_of_code="LEI",
                legal_name="Amazon Web Services EMEA SARL",
                person_type=TypeOfPerson.LEGAL_PERSON,
                headquarters_country="LU",
            )
        ],
        # B_05.02 keys on 0010/0020/0030/0050/0060, so 0040 has to carry
        # something or the row is nothing but keys and cannot be represented.
        supply_chain=[
            SupplyChainLink(
                arrangement_reference="CTR-1",
                service_type=ICTServiceType.S17,
                provider_code=LEI,
                type_of_code="LEI",
                rank=1,
            )
        ],
    )


def sample_paths() -> set[str]:
    with zipfile.ZipFile(SAMPLE) as archive:
        root = Path(archive.namelist()[0]).parts[0]
        return {n[len(root) + 1 :] for n in archive.namelist() if n[len(root) + 1 :] and not n.endswith("/")}


def rows_of(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


class TestGoldenStructure:
    """What we emit must have the same shape as the official sample."""

    def test_the_same_set_of_files(self) -> None:
        ours = {path.split("/", 1)[1] for path in build_package(register(), name())}
        assert ours == sample_paths()

    def test_everything_lives_under_one_directory_named_like_the_zip(self) -> None:
        roots = {path.split("/", 1)[0] for path in build_package(register(), name())}
        assert roots == {str(name())}

    def test_every_template_csv_has_the_official_header(self) -> None:
        with zipfile.ZipFile(SAMPLE) as archive:
            official = {
                Path(n).name: rows_of(archive.read(n).decode("utf-8-sig"))[0]
                for n in archive.namelist()
                if Path(n).name.startswith("b_")
            }
        ours = build_package(register(), name())
        for filename, header in official.items():
            mine = rows_of(ours[f"{name()}/reports/{filename}"])[0]
            assert set(mine) == set(header), filename

    def test_b_99_01_matches_too_now_that_it_is_modelled(self) -> None:
        with zipfile.ZipFile(SAMPLE) as archive:
            path = next(n for n in archive.namelist() if n.endswith("b_99.01.csv"))
            official = rows_of(archive.read(path).decode("utf-8-sig"))[0]
        mine = rows_of(build_package(register(), name())[f"{name()}/reports/b_99.01.csv"])[0]
        assert set(mine) == set(official)

    def test_parameters_has_the_same_keys_as_the_sample(self) -> None:
        with zipfile.ZipFile(SAMPLE) as archive:
            path = next(n for n in archive.namelist() if n.endswith("parameters.csv"))
            official = {row[0] for row in rows_of(archive.read(path).decode("utf-8-sig"))[1:]}
        ours = {row[0] for row in rows_of(build_package(register(), name())[f"{name()}/reports/parameters.csv"])[1:]}
        assert ours == official

    def test_filing_indicators_lists_every_template(self) -> None:
        text = build_package(register(), name())[f"{name()}/reports/FilingIndicators.csv"]
        rows = rows_of(text)
        assert rows[0] == ["templateID", "reported"]
        assert {row[0] for row in rows[1:]} == set(FIELD_CATALOG)


class TestTheFourThingsOnlyTheSampleCouldSettle:
    def test_headers_carry_the_c_prefix(self) -> None:
        header = rows_of(build_package(register(), name())[f"{name()}/reports/b_05.01.csv"])[0]
        assert "c0010" in header and "0010" not in header

    def test_closed_lists_are_emitted_as_qnames(self) -> None:
        rows = rows_of(build_package(register(), name())[f"{name()}/reports/b_05.02.csv"])
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["c0020"] == "eba_TA:S17"
        assert "Cloud" not in record["c0020"]

    def test_a_label_never_reaches_the_file(self) -> None:
        rows = rows_of(build_package(register(), name())[f"{name()}/reports/b_05.01.csv"])
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["c0070"] == "eba_CT:x212"

    def test_filing_indicators_are_upper_case_and_files_are_lower(self) -> None:
        package = build_package(register(), name())
        assert f"{name()}/reports/b_05.01.csv" in package
        assert "B_05.01,true" in package[f"{name()}/reports/FilingIndicators.csv"]

    def test_report_json_extends_the_dora_module(self) -> None:
        report = json.loads(build_package(register(), name())[f"{name()}/reports/report.json"])
        assert report["documentInfo"]["extends"] == [DORA_MODULE_URL]

    def test_report_package_json_declares_the_package_type(self) -> None:
        with zipfile.ZipFile(SAMPLE) as archive:
            path = next(n for n in archive.namelist() if n.endswith("reportPackage.json"))
            official = json.loads(archive.read(path))
        ours = json.loads(build_package(register(), name())[f"{name()}/META-INF/reportPackage.json"])
        assert ours == official


class TestCellFormatting:
    def test_an_empty_field_is_an_empty_cell(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="Acme")])
        rows = rows_of(build_package(roi, name())[f"{name()}/reports/b_05.01.csv"])
        assert rows[1].count("") == len(rows[1]) - 1

    def test_dates_are_iso(self) -> None:
        rows = rows_of(build_package(register(), name())[f"{name()}/reports/b_01.01.csv"])
        assert "2026-03-31" in rows[1]

    def test_money_keeps_its_scale_and_is_not_a_float(self) -> None:
        rows = rows_of(build_package(register(), name())[f"{name()}/reports/b_02.01.csv"])
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["c0050"] == "120000.50"

    def test_booleans_are_lower_case(self) -> None:
        from dora_roi.models.templates import ServiceAssessment

        roi = RegisterOfInformation(assessments=[ServiceAssessment(exit_plan_exists=True)])
        rows = rows_of(build_package(roi, name())[f"{name()}/reports/b_07.01.csv"])
        assert "true" in rows[1]

    def test_an_empty_template_still_gets_its_header(self) -> None:
        text = build_package(RegisterOfInformation(), name())[f"{name()}/reports/b_06.01.csv"]
        rows = rows_of(text)
        assert len(rows) == 1 and rows[0][0] == "c0010"

    def test_an_unreported_template_says_false_rather_than_going_silent(self) -> None:
        text = build_package(RegisterOfInformation(), name())[f"{name()}/reports/FilingIndicators.csv"]
        assert "B_06.01,false" in text

    def test_rows_are_crlf_terminated_like_the_sample(self) -> None:
        text = build_package(register(), name())[f"{name()}/reports/b_05.01.csv"]
        assert "\r\n" in text


class TestPackageName:
    def test_renders_the_documented_convention(self) -> None:
        assert str(name()) == f"{LEI}.CON_IT_DORA010100_DORA_2026-03-31_20260331120000000"

    def test_individual_scope(self) -> None:
        individual = PackageName(LEI, False, "IT", date(2026, 3, 31), "20260331120000000")
        assert ".IND_IT_" in str(individual)

    def test_matches_the_shape_of_the_official_sample(self) -> None:
        import re

        with zipfile.ZipFile(SAMPLE) as archive:
            official = Path(archive.namelist()[0]).parts[0]
        shape = re.compile(r"^[A-Z0-9]{20}\.(CON|IND)_[A-Z]{2}_DORA010100_DORA_\d{4}-\d{2}-\d{2}_\d{17}$")
        assert shape.match(official)
        assert shape.match(str(name()))

    @pytest.mark.parametrize("bad", ["NOT-A-LEI", "", "5493001KJTIIGC8Y1R1"])
    def test_a_bad_lei_is_refused(self, bad: str) -> None:
        with pytest.raises(ExportError, match="LEI"):
            PackageName(bad, True, "IT", date(2026, 3, 31), "20260331120000000")

    def test_a_bad_country_is_refused(self) -> None:
        with pytest.raises(ExportError, match="country"):
            PackageName(LEI, True, "ITA", date(2026, 3, 31), "20260331120000000")

    def test_a_bad_timestamp_is_refused(self) -> None:
        with pytest.raises(ExportError, match="timestamp"):
            PackageName(LEI, True, "IT", date(2026, 3, 31), "2026")


class TestWriteAndReadBack:
    def test_writes_a_zip_that_matches_the_sample_layout(self, tmp_path: Path) -> None:
        target = write_package(register(), name(), tmp_path)
        assert target.name == f"{name()}.zip"
        with zipfile.ZipFile(target) as archive:
            root = Path(archive.namelist()[0]).parts[0]
            paths = {n[len(root) + 1 :] for n in archive.namelist()}
        assert paths == sample_paths()

    def test_creates_the_output_directory(self, tmp_path: Path) -> None:
        assert write_package(register(), name(), tmp_path / "deep" / "nested").is_file()

    def test_generating_software_is_optional_and_omitted_by_default(self) -> None:
        report = json.loads(build_package(register(), name())[f"{name()}/reports/report.json"])
        assert "eba:generatingSoftwareInformation" not in report

    def test_generating_software_is_included_when_given(self) -> None:
        package = build_package(register(), name(), generating_software="dora-roi 0.1.0")
        report = json.loads(package[f"{name()}/reports/report.json"])
        assert report["eba:generatingSoftwareInformation"] == "dora-roi 0.1.0"

    def test_a_bad_base_currency_is_refused(self) -> None:
        with pytest.raises(ExportError, match="currency"):
            build_package(register(), name(), base_currency="EURO")


class TestKeyOnlyRows:
    """A row of nothing but key columns has no fact to attach to its key."""

    def test_it_is_refused_rather_than_written(self) -> None:
        roi = RegisterOfInformation(arrangements=[ContractualArrangementGeneral(arrangement_reference="CTR-1")])
        with pytest.raises(ExportError, match="only key columns"):
            build_package(roi, name())

    def test_the_message_names_the_keys(self) -> None:
        roi = RegisterOfInformation(arrangements=[ContractualArrangementGeneral(arrangement_reference="CTR-1")])
        with pytest.raises(ExportError, match="Keys here are 0010"):
            build_package(roi, name())

    def test_one_real_value_is_enough(self) -> None:
        roi = RegisterOfInformation(
            arrangements=[
                ContractualArrangementGeneral(
                    arrangement_reference="CTR-1", arrangement_type=ContractualArrangementType.STANDALONE
                )
            ]
        )
        assert build_package(roi, name())


class TestIdentifierTypeQNames:
    def test_a_type_of_code_files_as_a_qname(self) -> None:
        rows = rows_of(build_package(register(), name())[f"{name()}/reports/b_05.01.csv"])
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["c0020"] == "eba_qCO:qx2000"

    def test_an_unknown_type_of_code_is_refused(self) -> None:
        roi = RegisterOfInformation(
            providers=[ThirdPartyProvider(legal_name="Acme", identification_code="X", type_of_code="WAT")]
        )
        with pytest.raises(ExportError, match="type of identification code"):
            build_package(roi, name())


class TestIsoCodesAreClosedListsToo:
    def test_a_country_files_as_a_qname(self) -> None:
        rows = rows_of(build_package(register(), name())[f"{name()}/reports/b_05.01.csv"])
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["c0080"] == "eba_GA:LU"

    def test_a_currency_files_as_a_qname(self) -> None:
        rows = rows_of(build_package(register(), name())[f"{name()}/reports/b_02.01.csv"])
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["c0040"] == "eba_CU:EUR"

    def test_an_entity_type_files_as_a_qname_and_is_case_insensitive(self) -> None:
        roi = RegisterOfInformation(
            entity=EntityMaintainingRegister(lei=LEI, name="Acme", entity_type="Payment Institution")
        )
        rows = rows_of(build_package(roi, name())[f"{name()}/reports/b_01.01.csv"])
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["c0040"] == "eba_CT:x300"

    def test_an_entity_type_outside_the_list_is_refused(self) -> None:
        roi = RegisterOfInformation(entity=EntityMaintainingRegister(lei=LEI, name="Acme", entity_type="Bakery"))
        with pytest.raises(ExportError, match="closed list"):
            build_package(roi, name())
