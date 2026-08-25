"""Validate an exported package against the official EBA taxonomy, with Arelle.

This is the acceptance criterion that mattered — "passes an external
validator" — met locally. Nothing is uploaded anywhere: Arelle is the reference XBRL processor
and the EBA publishes its taxonomy, so the check runs inside the container.

It earns its keep. Run for the first time, it found four bugs that every other
test in this suite was blind to, because they are all invisible until something
actually resolves the taxonomy:

* country and currency codes filed bare instead of as `eba_GA:IT` / `eba_CU:EUR`
* `type_of_code` filed as `LEI` instead of `eba_qCO:qx2000`
* rows made only of key columns, which have no fact to attach the key to
* rank-1 supply-chain links with no recipient — a *key* column, so the row's
  context never closed

The taxonomy package is 18 MB and is not vendored. Without it these tests skip,
which keeps a plain `docker compose run --rm dev` fast and offline; CI and
anyone chasing an export bug fetch it once. See PROVENANCE.md.
"""

from __future__ import annotations

import subprocess
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dora_roi.export.xbrl_csv import PackageName, write_package
from dora_roi.models.enums import ContractualArrangementType, ICTServiceType, TypeOfPerson
from dora_roi.models.templates import (
    ContractualArrangementGeneral,
    EntityMaintainingRegister,
    RegisterOfInformation,
    SupplyChainLink,
    ThirdPartyProvider,
)

EBA = Path(__file__).parent / "fixtures" / "eba"
TAXONOMY = EBA / "taxo_package_4.0.zip"
SAMPLE = EBA / "sample_dora_package.zip"
ENTITY_LEI = "529900T8BM49AURSDO55"
PROVIDER_LEI = "5493001KJTIIGC8Y1R12"

pytestmark = pytest.mark.skipif(
    not TAXONOMY.is_file(),
    reason=f"{TAXONOMY.name} not fetched; see tests/fixtures/eba/PROVENANCE.md",
)


def taxonomy_packages(tmp_path: Path) -> str:
    """Arelle wants the three inner packages, pipe-separated."""
    extracted = tmp_path / "taxonomy"
    with zipfile.ZipFile(TAXONOMY) as archive:
        archive.extractall(extracted)
    inner = sorted(str(p) for p in extracted.glob("EBA_XBRL_*.zip"))
    assert len(inner) == 3, inner
    return "|".join(inner)


def validate(package: Path, packages: str) -> list[str]:
    """Every error Arelle reports. Empty means the package is structurally valid."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "python",
            "-m",
            "arelle.CntlrCmdLine",
            "--packages",
            packages,
            "--file",
            str(package),
            "--validate",
            "--logLevel",
            "error",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def filable_register() -> RegisterOfInformation:
    """The smallest register that is actually complete enough to be filed."""
    return RegisterOfInformation(
        entity=EntityMaintainingRegister(
            lei=ENTITY_LEI,
            name="Acme Payments SpA",
            country="IT",
            entity_type="payment institution",
            competent_authority="Banca d'Italia",
            reporting_date=date(2026, 3, 31),
        ),
        arrangements=[
            ContractualArrangementGeneral(
                arrangement_reference="CTR-2026-001",
                arrangement_type=ContractualArrangementType.STANDALONE,
                currency="EUR",
                annual_expense=Decimal("120000.00"),
            )
        ],
        providers=[
            ThirdPartyProvider(
                identification_code=PROVIDER_LEI,
                type_of_code="LEI",
                legal_name="Amazon Web Services EMEA SARL",
                name_latin="Amazon Web Services EMEA SARL",
                person_type=TypeOfPerson.LEGAL_PERSON,
                headquarters_country="LU",
                ultimate_parent_code=PROVIDER_LEI,
                type_of_parent_code="LEI",
            )
        ],
        supply_chain=[
            SupplyChainLink(
                arrangement_reference="CTR-2026-001",
                service_type=ICTServiceType.S17,
                provider_code=PROVIDER_LEI,
                type_of_code="LEI",
                rank=1,
                recipient_code=ENTITY_LEI,
                type_of_recipient_code="LEI",
            )
        ],
    )


@pytest.fixture(scope="module")
def packages(tmp_path_factory) -> str:
    return taxonomy_packages(tmp_path_factory.mktemp("eba"))


class TestAgainstTheOfficialTaxonomy:
    def test_the_official_sample_validates(self, packages: str) -> None:
        """The control. If this ever fails, the harness is wrong, not the code."""
        assert validate(SAMPLE, packages) == []

    def test_our_package_validates(self, tmp_path: Path, packages: str) -> None:
        target = write_package(filable_register(), _name(), tmp_path)
        assert validate(target, packages) == []


def _name() -> PackageName:
    return PackageName(
        lei=ENTITY_LEI,
        consolidated=True,
        country="IT",
        reference_date=date(2026, 3, 31),
        timestamp="20260331120000000",
    )
