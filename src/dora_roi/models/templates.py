"""Register of Information rows, their provenance, and the field catalog.

Three things live here.

**Rows.** One pydantic model per modelled template. Field aliases are the
official column codes, so ``model_dump(by_alias=True)`` already produces the
headers an exporter needs and nothing has to re-map them later.

**Provenance.** Every row carries, beside its values, where each value came
from (golden rule 1). The provenance dict is excluded from every dump: it is
how the tool reasons about its own output, and it must never reach a filing.

**The catalog.** ``FIELD_CATALOG`` describes all 15 templates — including the
11 with no model yet — so the gap report can enumerate what is missing from
day one rather than staying silent about templates it cannot represent.

Rows are deliberately permissive: every field is optional and an empty row is
legal. A prefill has holes by construction, and refusing to build one would
leave the gap report with nothing to report on. Mandatory-ness is a property of
the *catalog*, checked by the gap report, not a constructor precondition. What
the models do enforce is *shape*: an LEI that is not an LEI, a country that is
not ISO 3166-1 alpha-2, a rank below 1 — those raise at construction, because a
malformed value is worse than a missing one. It survives to the filing and gets
rejected there.

Field codes, names and mandatory flags began as a secondary reconstruction from
the ITS. Golden rule 6: the official EBA annotated template wins, and the test
suite reconciles this catalog against it on every run.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, ClassVar, NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from dora_roi.models.enums import (
    ContractualArrangementType,
    DataSensitiveness,
    FieldStatus,
    ICTServiceType,
    ImpactOfDiscontinuing,
    LevelOfReliance,
    ReasonNotSubstitutable,
    Reintegration,
    Substitutability,
    TypeOfPerson,
)

__all__ = [
    "FIELD_CATALOG",
    "LEI_PATTERN",
    "MODELLED_TEMPLATES",
    "UNVERIFIED_CLOSED_LISTS",
    "TEMPLATE_COLLECTIONS",
    "UNVERIFIED_CLOSED_LISTS",
    "UNVERIFIED_FIELD_NAMES",
    "Branch",
    "BusinessFunction",
    "ContractualArrangementGeneral",
    "ContractualArrangementSpecific",
    "CountryCode",
    "CurrencyCode",
    "Definitions",
    "EntityMaintainingRegister",
    "FieldSpec",
    "GroupEntity",
    "IntraGroupArrangement",
    "Lei",
    "Provenance",
    "RegisterOfInformation",
    "RoIRow",
    "ServiceAssessment",
    "ServiceUser",
    "SigningEntity",
    "SigningIntraGroupProvider",
    "SigningProvider",
    "SupplyChainLink",
    "ThirdPartyProvider",
]

LEI_PATTERN = r"^[A-Z0-9]{18}[0-9]{2}$"
_LEI_RE = re.compile(LEI_PATTERN)

#: ISO 17442 legal entity identifier: 18 alphanumerics plus 2 check digits.
Lei = Annotated[str, StringConstraints(pattern=LEI_PATTERN)]
#: ISO 3166-1 alpha-2.
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
#: ISO 4217.
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Provenance(BaseModel):
    """Where a single value came from, and how much it can be trusted."""

    status: FieldStatus
    source: str | None = None
    note: str | None = None


class RoIRow(BaseModel):
    """Base for every template row: official-code aliases plus provenance.

    ``populate_by_name`` lets callers build a row either way — ``lei=...`` when
    writing Python, ``**{"0010": ...}`` when reading something that already
    speaks official codes. ``extra="forbid"`` turns a mistyped field into an
    error instead of a value that silently never reaches the export.
    """

    # validate_assignment matters more here than at construction: the pipeline
    # builds an empty row and fills it field by field, so without it every
    # validator in this module would be dead code for the way rows are actually
    # populated, and a malformed LEI written by an enricher would sail through.
    model_config = ConfigDict(populate_by_name=True, extra="forbid", validate_assignment=True)

    #: Template code, e.g. ``"B_05.01"``. Set by each subclass.
    TEMPLATE: ClassVar[str] = ""

    provenance: dict[str, Provenance] = Field(default_factory=dict, exclude=True)

    #: What this row was built from — the Terraform provider name, usually.
    #: Excluded from every dump, like provenance: it is how the tool finds a row
    #: again, not something a filing ever sees. Without it the overlay had to
    #: match rows by list position, which held only because `scan` happened to
    #: build providers, arrangements and supply-chain links in one pass.
    source_key: str | None = Field(default=None, exclude=True)

    @classmethod
    def roi_fields(cls) -> tuple[str, ...]:
        """The row's reportable field names, provenance excluded."""
        return tuple(name for name, info in cls.model_fields.items() if info.alias is not None)

    def mark(
        self,
        field: str,
        status: FieldStatus,
        source: str | None = None,
        note: str | None = None,
    ) -> Self:
        """Record where ``field``'s value came from. Chainable.

        Raises on an unknown field name: a typo here would silently produce a
        provenance entry for a field that does not exist, and the gap report
        would go on calling the real field MISSING.
        """
        if field not in self.roi_fields():
            raise ValueError(f"{type(self).__name__} has no field {field!r}")
        self.provenance[field] = Provenance(status=status, source=source, note=note)
        return self

    def status_of(self, field: str) -> FieldStatus:
        """Status of ``field``; MISSING when nothing ever marked it."""
        entry = self.provenance.get(field)
        return entry.status if entry is not None else FieldStatus.MISSING


class EntityMaintainingRegister(RoIRow):
    """B_01.01 — the entity maintaining the register. One row per filing."""

    TEMPLATE: ClassVar[str] = "B_01.01"

    lei: Lei | None = Field(default=None, alias="0010")
    name: str | None = Field(default=None, alias="0020")
    country: CountryCode | None = Field(default=None, alias="0030")
    entity_type: str | None = Field(default=None, alias="0040")
    competent_authority: str | None = Field(default=None, alias="0050")
    reporting_date: date | None = Field(default=None, alias="0060")


class ContractualArrangementGeneral(RoIRow):
    """B_02.01 — contractual arrangements, general. The arrangement-reference hub."""

    TEMPLATE: ClassVar[str] = "B_02.01"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    arrangement_type: ContractualArrangementType | None = Field(default=None, alias="0020")
    overarching_reference: str | None = Field(default=None, alias="0030")
    currency: CurrencyCode | None = Field(default=None, alias="0040")
    annual_expense: Decimal | None = Field(default=None, alias="0050")


class ThirdPartyProvider(RoIRow):
    """B_05.01 — ICT third-party service providers. The provider hub, and the MVP's core."""

    TEMPLATE: ClassVar[str] = "B_05.01"

    identification_code: str | None = Field(default=None, alias="0010")
    type_of_code: str | None = Field(default=None, alias="0020")
    additional_code: str | None = Field(default=None, alias="0030")
    type_of_additional_code: str | None = Field(default=None, alias="0040")
    legal_name: str | None = Field(default=None, alias="0050")
    name_latin: str | None = Field(default=None, alias="0060")
    person_type: TypeOfPerson | None = Field(default=None, alias="0070")
    headquarters_country: CountryCode | None = Field(default=None, alias="0080")
    currency: CurrencyCode | None = Field(default=None, alias="0090")
    total_annual_expense: Decimal | None = Field(default=None, alias="0100")
    ultimate_parent_code: str | None = Field(default=None, alias="0110")
    type_of_parent_code: str | None = Field(default=None, alias="0120")

    @model_validator(mode="after")
    def _codes_must_match_their_declared_type(self) -> Self:
        """An identification code typed ``LEI`` has to actually be one.

        The code field is a plain string because the closed list also allows
        EUID and national codes, whose shapes differ by country. But invalid
        LEIs caused roughly a third of the 2025 first-filing failures, which is
        worth catching at construction rather than at the regulator.
        """
        pairs = (
            ("identification_code", "type_of_code"),
            ("ultimate_parent_code", "type_of_parent_code"),
        )
        for code_field, type_field in pairs:
            code = getattr(self, code_field)
            if code is not None and getattr(self, type_field) == "LEI" and not _LEI_RE.fullmatch(code):
                raise ValueError(f"{code_field} is typed LEI but {code!r} is not a valid LEI (ISO 17442)")
        return self


class SupplyChainLink(RoIRow):
    """B_05.02 — ICT service supply chains. Rank 1 is the direct provider."""

    TEMPLATE: ClassVar[str] = "B_05.02"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    service_type: ICTServiceType | None = Field(default=None, alias="0020")
    provider_code: str | None = Field(default=None, alias="0030")
    type_of_code: str | None = Field(default=None, alias="0040")
    rank: int | None = Field(default=None, alias="0050", ge=1)
    recipient_code: str | None = Field(default=None, alias="0060")
    type_of_recipient_code: str | None = Field(default=None, alias="0070")


#: The templates that have a row model. The gap report is row-aware for these
#: and emits synthetic all-MISSING entries for the rest.
MODELLED_TEMPLATES: dict[str, type[RoIRow]] = {
    "B_01.01": EntityMaintainingRegister,
    "B_02.01": ContractualArrangementGeneral,
    "B_05.01": ThirdPartyProvider,
    "B_05.02": SupplyChainLink,
}


class FieldSpec(NamedTuple):
    """One column of one template.

    ``condition`` earns its slot in what would otherwise be a three-tuple:
    B_02.02.0150 is mandatory *only when data is stored*.
    Flattening that to ``True`` would make the gap report declare a blocking
    gap on every arrangement that stores nothing, and flattening it to
    ``False`` would hide a real one. Neither is a report worth reading.
    """

    code: str
    name: str
    mandatory: bool
    condition: str | None = None


#: Templates not reconciled against the official EBA annotated layout. Empty
#: since the reconciliation: all fifteen now match it and the sample package, and
#: `test_eba_reconciliation.py` re-derives that on every run.
UNVERIFIED_FIELD_NAMES: frozenset[str] = frozenset()

_F = FieldSpec

FIELD_CATALOG: dict[str, list[FieldSpec]] = {
    "B_01.01": [
        _F("0010", "LEI of the entity maintaining the register of information", True),
        _F("0020", "Name of the entity", True),
        _F("0030", "Country of the entity", True),
        _F("0040", "Type of entity", True),
        _F("0050", "Competent Authority", True),
        _F("0060", "Date of the reporting", True),
    ],
    "B_01.02": [
        _F("0010", "LEI of the entity", True),
        _F("0020", "Name of the entity", True),
        _F("0030", "Country of the entity", True),
        _F("0040", "Type of entity", True),
        _F("0050", "Hierarchy of the entity within the group (where applicable)", True),
        _F("0060", "LEI of the direct parent undertaking of the entity", False),
        _F("0070", "Date of last update", True),
        _F("0080", "Date of integration in the Register of information", True),
        _F("0090", "Date of deletion in the Register of information", False),
        _F("0100", "Currency", False),
        _F("0110", "Value of total assets - of the financial entity", False),
    ],
    "B_01.03": [
        _F("0010", "Identification code of the branch", True),
        _F("0020", "LEI of the financial entity head office of the branch", True),
        _F("0030", "Name of the branch", True),
        _F("0040", "Country of the branch", True),
    ],
    "B_02.01": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "Type of contractual arrangement", True),
        _F("0030", "Overarching contractual arrangement reference number", False),
        _F("0040", "Currency of the amount reported in RT.02.01.0050", False),
        _F("0050", "Annual expense or estimated cost of the contractual arrangement for the past year", False),
    ],
    "B_02.02": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "LEI of the entity making use of the ICT service(s)", True),
        _F("0030", "Identification code of the ICT third-party service provider", True),
        _F("0040", "Type of code to identify the ICT third-party service provider", False),
        _F("0050", "Function identifier", True),
        _F("0060", "Type of ICT services", True),
        _F("0070", "Start date of the contractual arrangement", False),
        _F("0080", "End date of the contractual arrangement", False),
        _F("0090", "Reason of the termination or ending of the contractual arrangement", False),
        _F("0100", "Notice period for the financial entity making use of the ICT service(s)", False),
        _F("0110", "Notice period for the ICT third-party service provider", False),
        _F("0120", "Country of the governing law of the contractual arrangement", False),
        _F("0130", "Country of provision of the ICT services", True),
        _F("0140", "Storage of data", False),
        _F(
            "0150",
            "Location of the data at rest (storage)",
            True,
            condition="only when B_02.02.0140 (storage of data) is yes",
        ),
        _F("0160", "Location of management of the data (processing)", True),
        _F("0170", "Sensitiveness of the data stored by the ICT third-party service provider", False),
        _F("0180", "Level of reliance on the ICT service supporting the critical or important function.", False),
    ],
    "B_02.03": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "Contractual arrangement linked to the contractual arrangement referred in RT.02.03.0010", True),
        _F("0030", "Link", True),
    ],
    "B_03.01": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "LEI of the entity signing the contractual arrangement", True),
        _F("0030", "Link", True),
    ],
    "B_03.02": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "Identification code of ICT third-party service provider", True),
        _F("0030", "Type of code to identify the ICT third-party service provider", True),
    ],
    "B_03.03": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "LEI of the entity providing ICT services", True),
        _F("0031", "Link", True),
    ],
    "B_04.01": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "LEI of the entity making use of the ICT service(s)", True),
        _F("0030", "Nature of the entity making use of the ICT service(s)", True),
        _F("0040", "Identification code of the branch", False),
    ],
    "B_05.01": [
        _F("0010", "Identification code of ICT third-party service provider", True),
        _F("0020", "Type of code to identify the ICT third-party service provider", True),
        _F("0030", "Additional identification code of ICT third-party service provider", False),
        _F("0040", "Type of additional identification code of the ICT third-party service provider", False),
        _F("0050", "Legal name of the ICT third-party service provider", True),
        _F("0060", "Name of the ICT third-party service provider in Latin alphabet", False),
        _F("0070", "Type of person of the ICT third-party service provider", True),
        _F("0080", "Country of the ICT third-party service provider’s headquarters", True),
        _F("0090", "Currency of the amount reported in RT.05.01.0070", False),
        _F("0100", "Total annual expense or estimated cost of the ICT third-party service provider", False),
        _F("0110", "Identification code of the ICT third-party service provider’s ultimate parent undertaking", True),
        _F(
            "0120", "Type of code to identify the ICT third-party service provider’s ultimate parent undertaking", False
        ),
    ],
    "B_05.02": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "Type of ICT services", True),
        _F("0030", "Identification code of the ICT third-party service provider", True),
        _F("0040", "Type of code to identify the ICT third-party service provider", False),
        _F("0050", "Rank", True),
        _F("0060", "Identification code of the recipient of sub-contracted ICT services", False),
        _F("0070", "Type of code to identify the recipient of sub-contracted ICT services", False),
    ],
    "B_06.01": [
        _F("0010", "Function Identifier", True),
        _F("0020", "Licenced activity", True),
        _F("0030", "Function name", True),
        _F("0040", "LEI of the financial entity", True),
        _F("0050", "Criticality or importance assessment", True),
        _F("0060", "Reasons for criticality or importance", False),
        _F("0070", "Date of the last assessment of criticality or importance", False),
        _F("0080", "Recovery time objective of the function", False),
        _F("0090", "Recovery point objective of the function", False),
        _F("0100", "Impact of discontinuing the function", False),
    ],
    "B_07.01": [
        _F("0010", "Contractual arrangement reference number", True),
        _F("0020", "Identification code of the ICT third-party service provider", True),
        _F("0030", "Type of code to identify the ICT third-party service provider", False),
        _F("0040", "Type of ICT services", True),
        _F("0050", "Substitutability of the ICT third-party service provider", True),
        _F(
            "0060",
            # Verbatim from the EBA annotated layout, and the reconciliation test
            # compares it character for character. Reflowing it would be a lie.
            "Reason if the ICT third-party service provider is considered not substitutable or difficult to be substitutable",  # noqa: E501
            False,
        ),
        _F("0070", "Date of the last audit on the ICT third-party service provider", False),
        _F("0080", "Existence of an exit plan", True),
        _F("0090", "Possibility of reintegration of the contracted ICT service", False),
        _F("0100", "Impact of discontinuing the ICT services", True),
        _F("0110", "Are there alternative ICT third-party service providers identified?", False),
        _F("0120", "Identification of alternative ICT TPP", False),
    ],
    "B_99.01": [
        _F("0010", "Standalone arrangement", False),
        _F("0020", "Overarching arrangement", False),
        _F("0030", "Subsequent or associated arrangement", False),
        _F("0040", "Low", False),
        _F("0050", "Medium", False),
        _F("0060", "High", False),
        _F("0070", "Low", False),
        _F("0080", "Medium", False),
        _F("0090", "High", False),
        _F("0100", "Not substitutable", False),
        _F("0110", "Highly complex substitutability", False),
        _F("0120", "Medium complexity in terms of substitutability", False),
        _F("0130", "Easily substitutable", False),
        _F("0140", "Easy", False),
        _F("0150", "Difficult", False),
        _F("0160", "Highly complex", False),
        _F("0170", "Low", False),
        _F("0180", "Medium", False),
        _F("0190", "High", False),
    ],
}


# ---------------------------------------------------------------------------
# The remaining eleven templates.
#
# Two closed lists stay `str` here, deliberately. The drafting notes say
# B_01.01.0040 / B_01.02.0040 use a list of 22 entity types and that B_01.02.0050
# uses one for the hierarchy, but they do not enumerate either. Inventing the
# members would produce a validator that rejects correct filings, which is worse
# than one that accepts a wrong string the gap report can still flag. Both were
# left for the reconciliation to resolve against the EBA files.
# ---------------------------------------------------------------------------

#: Closed lists still lacking an official source. Empty since the reconciliation:
#: every list the drafting notes named without enumerating — the 22 entity types,
#: the 24 for B_01.02,
#: the hierarchy, nature of entity, criticality and the 131 licenced activities —
#: now ships in `data/eba_closed_lists.json`, extracted from the EBA workbook.
#:
#: The fields above stay typed `str` rather than becoming enums. Those lists run
#: to hundreds of values and a model that rejects anything outside our extraction
#: would refuse correct filings; membership is a pre-flight check, where being
#: wrong costs a warning instead of a crash.
UNVERIFIED_CLOSED_LISTS: frozenset[str] = frozenset()


class GroupEntity(RoIRow):
    """B_01.02 — entities within the scope of consolidation."""

    TEMPLATE: ClassVar[str] = "B_01.02"

    lei: Lei | None = Field(default=None, alias="0010")
    name: str | None = Field(default=None, alias="0020")
    country: CountryCode | None = Field(default=None, alias="0030")
    entity_type: str | None = Field(default=None, alias="0040")
    hierarchy: str | None = Field(default=None, alias="0050")
    direct_parent_lei: Lei | None = Field(default=None, alias="0060")
    last_update: date | None = Field(default=None, alias="0070")
    integration_date: date | None = Field(default=None, alias="0080")
    deletion_date: date | None = Field(default=None, alias="0090")
    currency: CurrencyCode | None = Field(default=None, alias="0100")
    total_assets: Decimal | None = Field(default=None, alias="0110")


class Branch(RoIRow):
    """B_01.03 — branches. Foreign key into B_01.02 via the head office LEI."""

    TEMPLATE: ClassVar[str] = "B_01.03"

    branch_code: str | None = Field(default=None, alias="0010")
    head_office_lei: Lei | None = Field(default=None, alias="0020")
    name: str | None = Field(default=None, alias="0030")
    country: CountryCode | None = Field(default=None, alias="0040")


class ContractualArrangementSpecific(RoIRow):
    """B_02.02 — contractual arrangements, specific. The widest template in the register."""

    TEMPLATE: ClassVar[str] = "B_02.02"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    using_entity_lei: Lei | None = Field(default=None, alias="0020")
    provider_code: str | None = Field(default=None, alias="0030")
    type_of_code: str | None = Field(default=None, alias="0040")
    function_identifier: str | None = Field(default=None, alias="0050")
    service_type: ICTServiceType | None = Field(default=None, alias="0060")
    start_date: date | None = Field(default=None, alias="0070")
    end_date: date | None = Field(default=None, alias="0080")
    termination_reason: str | None = Field(default=None, alias="0090")
    notice_period_entity: int | None = Field(default=None, alias="0100", ge=0)
    notice_period_provider: int | None = Field(default=None, alias="0110", ge=0)
    governing_law_country: CountryCode | None = Field(default=None, alias="0120")
    country_of_provision: CountryCode | None = Field(default=None, alias="0130")
    storage_of_data: bool | None = Field(default=None, alias="0140")
    location_of_data_at_rest: CountryCode | None = Field(default=None, alias="0150")
    location_of_data_management: CountryCode | None = Field(default=None, alias="0160")
    data_sensitiveness: DataSensitiveness | None = Field(default=None, alias="0170")
    level_of_reliance: LevelOfReliance | None = Field(default=None, alias="0180")

    @model_validator(mode="after")
    def _dates_must_run_forwards(self) -> Self:
        """An arrangement that ends before it starts is a typo, not a contract."""
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError(f"end date {self.end_date} precedes start date {self.start_date}")
        return self


class IntraGroupArrangement(RoIRow):
    """B_02.03 — list of intra-group contractual arrangements.

    0020 is not a provider LEI, which is what the drafting notes said: the EBA layout names
    it "Contractual arrangement linked to the contractual arrangement referred in
    RT.02.03.0010". The template links one arrangement to another, and modelling
    it as an LEI would have made every intra-group row unfileable.
    """

    TEMPLATE: ClassVar[str] = "B_02.03"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    linked_arrangement_reference: str | None = Field(default=None, alias="0020")
    link: bool | None = Field(default=None, alias="0030")


class SigningEntity(RoIRow):
    """B_03.01 — the financial entities that signed. Link table."""

    TEMPLATE: ClassVar[str] = "B_03.01"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    entity_lei: Lei | None = Field(default=None, alias="0020")
    link: bool | None = Field(default=None, alias="0030")


class SigningProvider(RoIRow):
    """B_03.02 — the third-party providers that signed. Link table."""

    TEMPLATE: ClassVar[str] = "B_03.02"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    provider_code: str | None = Field(default=None, alias="0020")
    type_of_code: str | None = Field(default=None, alias="0030")


class SigningIntraGroupProvider(RoIRow):
    """B_03.03 — entities within the group signing to provide ICT services.

    The link column is ``0031``, not ``0030``. That looks like a typo and is not:
    it is what the EBA annotated layout and the sample package both carry.
    """

    TEMPLATE: ClassVar[str] = "B_03.03"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    provider_lei: Lei | None = Field(default=None, alias="0020")
    link: bool | None = Field(default=None, alias="0031")


class ServiceUser(RoIRow):
    """B_04.01 — which entity uses which service."""

    TEMPLATE: ClassVar[str] = "B_04.01"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    entity_lei: Lei | None = Field(default=None, alias="0020")
    nature_of_entity: str | None = Field(default=None, alias="0030")
    branch_code: str | None = Field(default=None, alias="0040")


class BusinessFunction(RoIRow):
    """B_06.01 — the business functions the ICT services support.

    The drafting notes recorded these codes as jumping from 0090 to 0110 with no 0100. It
    was a transcription error: the official code is 0100 and 0110 does not
    exist. Transcribing the gap faithfully instead of quietly renumbering is
    what kept it visible until the EBA files could settle it.
    """

    TEMPLATE: ClassVar[str] = "B_06.01"

    function_identifier: str | None = Field(default=None, alias="0010")
    licenced_activity: str | None = Field(default=None, alias="0020")
    name: str | None = Field(default=None, alias="0030")
    entity_lei: Lei | None = Field(default=None, alias="0040")
    criticality: str | None = Field(default=None, alias="0050")
    criticality_reasons: str | None = Field(default=None, alias="0060")
    last_assessment_date: date | None = Field(default=None, alias="0070")
    recovery_time_objective: int | None = Field(default=None, alias="0080", ge=0)
    recovery_point_objective: int | None = Field(default=None, alias="0090", ge=0)
    impact_of_discontinuing: ImpactOfDiscontinuing | None = Field(default=None, alias="0100")


class ServiceAssessment(RoIRow):
    """B_07.01 — assessment of the ICT services supporting critical functions.

    Judgement data throughout: substitutability, exit plans, alternatives. No
    collector will ever fill a row of this, which is precisely why the gap
    report has to keep showing it.
    """

    TEMPLATE: ClassVar[str] = "B_07.01"

    arrangement_reference: str | None = Field(default=None, alias="0010")
    provider_code: str | None = Field(default=None, alias="0020")
    type_of_code: str | None = Field(default=None, alias="0030")
    service_type: ICTServiceType | None = Field(default=None, alias="0040")
    substitutability: Substitutability | None = Field(default=None, alias="0050")
    reason_not_substitutable: ReasonNotSubstitutable | None = Field(default=None, alias="0060")
    last_audit_date: date | None = Field(default=None, alias="0070")
    exit_plan_exists: bool | None = Field(default=None, alias="0080")
    reintegration: Reintegration | None = Field(default=None, alias="0090")
    impact_of_discontinuing: ImpactOfDiscontinuing | None = Field(default=None, alias="0100")
    alternatives_identified: bool | None = Field(default=None, alias="0110")
    alternative_providers: str | None = Field(default=None, alias="0120")


class Definitions(RoIRow):
    """B_99.01 — the entity's own internal definitions of the closed-list terms.

    The drafting notes described this as three columns of (term, description, reference).
    It is not: it is a *single row* with one free-text column per closed-list
    member, where a financial entity writes what that term means inside its own
    organisation. What does "Material reliance" mean to us; what counts as
    "Low" data sensitiveness here. Nineteen columns across six lists, and the
    reader of the register uses them to interpret every other template.

    Nothing here is marked mandatory. No active EBA validation rule covers this
    table and no document states its obligations, and inventing one would put a
    false blocker in the gap report.
    """

    TEMPLATE: ClassVar[str] = "B_99.01"

    # Type of contractual arrangement (B_02.01.0020)
    arrangement_standalone: str | None = Field(default=None, alias="0010")
    arrangement_overarching: str | None = Field(default=None, alias="0020")
    arrangement_subsequent: str | None = Field(default=None, alias="0030")

    # Sensitiveness of the data stored (B_02.02.0170)
    data_sensitiveness_low: str | None = Field(default=None, alias="0040")
    data_sensitiveness_medium: str | None = Field(default=None, alias="0050")
    data_sensitiveness_high: str | None = Field(default=None, alias="0060")

    # Impact of discontinuing the *function* (B_06.01.0100). Note the codes:
    # function impact is 0070-0090 and service impact is 0170-0190, which is not
    # the order the column labels suggest — both read "Low / Medium / High".
    function_impact_low: str | None = Field(default=None, alias="0070")
    function_impact_medium: str | None = Field(default=None, alias="0080")
    function_impact_high: str | None = Field(default=None, alias="0090")

    # Substitutability assessment (B_07.01.0050)
    substitutability_not: str | None = Field(default=None, alias="0100")
    substitutability_highly_complex: str | None = Field(default=None, alias="0110")
    substitutability_medium_complexity: str | None = Field(default=None, alias="0120")
    substitutability_easy: str | None = Field(default=None, alias="0130")

    # Possibility of reintegration (B_07.01.0090)
    reintegration_easy: str | None = Field(default=None, alias="0140")
    reintegration_difficult: str | None = Field(default=None, alias="0150")
    reintegration_highly_complex: str | None = Field(default=None, alias="0160")

    # Impact of discontinuing the *ICT services* (B_07.01.0100)
    service_impact_low: str | None = Field(default=None, alias="0170")
    service_impact_medium: str | None = Field(default=None, alias="0180")
    service_impact_high: str | None = Field(default=None, alias="0190")


MODELLED_TEMPLATES.update(
    {
        "B_01.02": GroupEntity,
        "B_01.03": Branch,
        "B_02.02": ContractualArrangementSpecific,
        "B_02.03": IntraGroupArrangement,
        "B_03.01": SigningEntity,
        "B_03.02": SigningProvider,
        "B_03.03": SigningIntraGroupProvider,
        "B_04.01": ServiceUser,
        "B_06.01": BusinessFunction,
        "B_07.01": ServiceAssessment,
        "B_99.01": Definitions,
    }
)


class RegisterOfInformation(BaseModel):
    """The whole register: every one of the fifteen templates has a home here.

    Defined last because it holds every row class. The four collections from
    phase 1 keep their names — `scan` and the gap report already speak them —
    and the eleven later templates join them rather than replacing anything.
    """

    entity: EntityMaintainingRegister | None = None
    group_entities: list[GroupEntity] = Field(default_factory=list)
    branches: list[Branch] = Field(default_factory=list)
    arrangements: list[ContractualArrangementGeneral] = Field(default_factory=list)
    arrangement_details: list[ContractualArrangementSpecific] = Field(default_factory=list)
    intra_group_arrangements: list[IntraGroupArrangement] = Field(default_factory=list)
    signing_entities: list[SigningEntity] = Field(default_factory=list)
    signing_providers: list[SigningProvider] = Field(default_factory=list)
    signing_intra_group: list[SigningIntraGroupProvider] = Field(default_factory=list)
    service_users: list[ServiceUser] = Field(default_factory=list)
    providers: list[ThirdPartyProvider] = Field(default_factory=list)
    supply_chain: list[SupplyChainLink] = Field(default_factory=list)
    functions: list[BusinessFunction] = Field(default_factory=list)
    assessments: list[ServiceAssessment] = Field(default_factory=list)
    definitions: list[Definitions] = Field(default_factory=list)


#: template -> the RegisterOfInformation attribute holding its rows. One place
#: to state the relationship, so the gap report and the exporter cannot disagree
#: about where a template's rows live.
TEMPLATE_COLLECTIONS: dict[str, str] = {
    "B_01.01": "entity",
    "B_01.02": "group_entities",
    "B_01.03": "branches",
    "B_02.01": "arrangements",
    "B_02.02": "arrangement_details",
    "B_02.03": "intra_group_arrangements",
    "B_03.01": "signing_entities",
    "B_03.02": "signing_providers",
    "B_03.03": "signing_intra_group",
    "B_04.01": "service_users",
    "B_05.01": "providers",
    "B_05.02": "supply_chain",
    "B_06.01": "functions",
    "B_07.01": "assessments",
    "B_99.01": "definitions",
}
