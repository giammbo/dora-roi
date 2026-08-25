"""Closed lists used across the DORA Register of Information.

These are *filing* values: the strings below are what an xBRL-CSV export puts on
the wire, so they are not cosmetic labels. They began as a
reconstruction from the ITS and secondary sources. Golden rule 6 applies — the
official EBA annotated template and sample package win, and the test suite
reconciles this module against them on every run.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from functools import cache
from importlib import resources

__all__ = [
    "CLOSED_LIST_FILE",
    "ContractualArrangementType",
    "ImpactOfDiscontinuing",
    "ReasonNotSubstitutable",
    "Reintegration",
    "DataSensitiveness",
    "FieldStatus",
    "ICTServiceType",
    "IdentifierType",
    "LevelOfReliance",
    "Priority",
    "Substitutability",
    "TypeOfPerson",
]


CLOSED_LIST_FILE = "eba_closed_lists.json"

#: enum class name -> the list it draws its labels from, in the packaged file.
#: A dict rather than a class attribute: an annotated assignment inside an Enum
#: body becomes a *member*, which would put a fake `EBA_LIST` in every list.
_LIST_BY_ENUM = {
    "ICTServiceType": "ict_service_type",
    "IdentifierType": "identifier_type",
    "TypeOfPerson": "type_of_person",
    "ContractualArrangementType": "contractual_arrangement",
    "DataSensitiveness": "data_sensitiveness",
    "LevelOfReliance": "level_of_reliance",
    "Substitutability": "substitutability",
    "ReasonNotSubstitutable": "reason_not_substitutable",
    "Reintegration": "reintegration",
    "ImpactOfDiscontinuing": "impact_of_discontinuing",
}


@cache
def _qnames(list_name: str) -> dict[str, str]:
    """label -> ``eba_`` QName, from the packaged EBA closed lists."""
    raw = resources.files("dora_roi.data").joinpath(CLOSED_LIST_FILE).read_text(encoding="utf-8")
    return {value["label"]: value["qname"] for value in json.loads(raw)[list_name]["values"]}


class _EbaCoded(StrEnum):
    """A closed list whose members carry an official ``eba_`` QName.

    The values in these enums are human-readable labels. That is right for a gap
    report and wrong for a filing, which carries a QName instead — ``eba_TA:S17``,
    ``eba_CT:x212``. The mapping between the two is the EBA's, not ours, so it is
    read from the official list at :data:`CLOSED_LIST_FILE` rather than written
    out by hand. A label that does not resolve is a label we got wrong, and
    ``test_eba_reconciliation.py`` fails on it.
    """

    @property
    def label(self) -> str:
        """The official wording. Same as the value, except where a short key reads better."""
        return self.value

    @property
    def qname(self) -> str:
        """What a filing carries for this member."""
        return _qnames(_LIST_BY_ENUM[type(self).__name__])[self.label]


class ICTServiceType(_EbaCoded):
    """Annex III ICT service types S01-S19.

    Used by B_02.02.0060, B_05.02.0020 and B_07.01.0040. Choosing the code for a
    given provider is the user's regulatory responsibility (golden rule 4): the
    packaged provider mapping only ever suggests, and always as ``INFERRED``.
    """

    description: str

    def __new__(cls, code: str, description: str) -> ICTServiceType:
        member = str.__new__(cls, code)
        member._value_ = code
        member.description = description
        return member

    S01 = "S01", "ICT project management"
    S02 = "S02", "ICT Development"
    S03 = "S03", "ICT help desk and first level support"
    S04 = "S04", "ICT security management services"
    S05 = "S05", "Provision of data"
    S06 = "S06", "Data analysis"
    S07 = "S07", "ICT, facilities and hosting services (excluding Cloud services)"
    S08 = "S08", "Computation"
    S09 = "S09", "Non-Cloud Data storage"
    S10 = "S10", "Telecom carrier"
    S11 = "S11", "Network infrastructure"
    S12 = "S12", "Hardware and physical devices"
    S13 = "S13", "Software licencing (excluding SaaS)"
    S14 = "S14", "ICT operation management (including maintenance)"
    S15 = "S15", "ICT Consulting"
    S16 = "S16", "ICT Risk management"
    S17 = "S17", "Cloud services: IaaS"
    S18 = "S18", "Cloud services: PaaS"
    S19 = "S19", "Cloud services: SaaS"

    @property
    def label(self) -> str:
        return self.description


_ISO2_RE = re.compile(r"[A-Z]{2}")
_NATIONAL_CODE_RE = re.compile(r"([A-Z]{2})_(CRN|VAT|PNR|NIN)")


class IdentifierType(_EbaCoded):
    """Type of identification code (B_05.01.0020, B_02.02.0040, B_05.02.0040).

    Two shapes coexist in this closed list: the standalone codes ``LEI`` and
    ``EUID``, and the national codes, which are only valid country-prefixed
    (``IT_VAT``, not ``VAT``). :meth:`code` composes them and :meth:`parse` reads
    them back, so callers never hand-assemble the string.

    Two constraints are *not* enforced here, because they depend on the
    provider and belong to the row validators and pre-flight:
    legal persons take only LEI or EUID, and non-EU providers only LEI.
    """

    LEI = "LEI"
    EUID = "EUID"
    CRN = "CRN"
    VAT = "VAT"
    PNR = "PNR"
    NIN = "NIN"

    @property
    def label(self) -> str:
        """The EBA wording, which is far too long to type into a YAML file."""
        return _IDENTIFIER_LABELS[self]

    @property
    def requires_country(self) -> bool:
        """True for national codes, which are meaningless without a country."""
        return self not in (IdentifierType.LEI, IdentifierType.EUID)

    def code(self, country: str | None = None) -> str:
        """Return the filing value, e.g. ``"LEI"`` or ``"IT_VAT"``."""
        if not self.requires_country:
            if country is not None:
                raise ValueError(f"{self.value} is not country-scoped: drop the country argument")
            return self.value
        if country is None:
            raise ValueError(f"{self.value} is a national code: it needs an ISO 3166-1 alpha-2 country")
        if not _ISO2_RE.fullmatch(country):
            raise ValueError(f"country must be an upper-case ISO 3166-1 alpha-2 code, got {country!r}")
        return f"{country}_{self.value}"

    @classmethod
    def parse(cls, raw: str) -> tuple[IdentifierType, str | None]:
        """Split a filing value into its type and country, inverse of :meth:`code`."""
        if raw in (cls.LEI.value, cls.EUID.value):
            return cls(raw), None
        match = _NATIONAL_CODE_RE.fullmatch(raw)
        if match is None:
            raise ValueError(f"{raw!r} is not a valid identifier type: expected LEI, EUID, or <ISO2>_<CRN|VAT|PNR|NIN>")
        return cls(match.group(2)), match.group(1)


#: The official list is six values with wording nobody would hand-type. The short
#: keys above stay the enum values; these are what they mean to the EBA. The
#: drafting notes called them PNR and NIN; the official list calls them Passport
#: Number and National code, which is the same pair under different names.
_IDENTIFIER_LABELS = {
    IdentifierType.LEI: "Legal Entity Identfier (LEI)",
    IdentifierType.EUID: "European Unified ID (EUID)",
    IdentifierType.CRN: "Company registration number (CRN)",
    IdentifierType.VAT: "Value added tax identification number (VAT)",
    IdentifierType.PNR: "Passport Number",
    IdentifierType.NIN: "National code",
}


class TypeOfPerson(_EbaCoded):
    """Type of person of the provider (B_05.01.0070).

    The scan defaults to :attr:`LEGAL_PERSON`, always marked ``INFERRED``.
    """

    LEGAL_PERSON = "Legal person, excluding individual acting in a business capacity"
    INDIVIDUAL = "Individual acting in a business capacity"


class ContractualArrangementType(_EbaCoded):
    """Type of contractual arrangement (B_02.01.0020)."""

    STANDALONE = "Standalone arrangement"
    OVERARCHING = "Overarching arrangement"
    SUBSEQUENT = "Subsequent or associated arrangement"


class LevelOfReliance(_EbaCoded):
    """Level of reliance on the ICT service (B_02.02.0180). MANUAL: overlay only."""

    NOT_SIGNIFICANT = "Not significant"
    LOW = "Low reliance"
    MATERIAL = "Material reliance"
    FULL = "Full reliance"


class DataSensitiveness(_EbaCoded):
    """Sensitiveness of the data stored (B_02.02.0170).

    Where several sensitivities apply to one arrangement, the highest wins.
    """

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class Substitutability(_EbaCoded):
    """Substitutability of the ICT provider (B_07.01.0050). MANUAL: judgement data."""

    NOT_SUBSTITUTABLE = "Not substitutable"
    HIGHLY_COMPLEX = "Highly complex substitutability"
    MEDIUM_COMPLEXITY = "Medium complexity in terms of substitutability"
    EASILY_SUBSTITUTABLE = "Easily substitutable"


class ReasonNotSubstitutable(_EbaCoded):
    """Why a provider cannot easily be replaced (B_07.01.0060). MANUAL."""

    NO_ALTERNATIVES = "Lack of real alternatives"
    MIGRATION_DIFFICULTY = "Difficulties in migrating or reintegrating"
    BOTH = "Lack of real alternatives and difficulties in migrating or reintegrating"


class Reintegration(_EbaCoded):
    """How hard it would be to bring the service back in house (B_07.01.0090). MANUAL."""

    EASY = "Easy"
    DIFFICULT = "Difficult"
    HIGHLY_COMPLEX = "Highly complex"
    NOT_APPLICABLE = "Not applicable"


class ImpactOfDiscontinuing(_EbaCoded):
    """Impact of losing the function or the service (B_06.01.0110, B_07.01.0100).

    ``ASSESSMENT_NOT_PERFORMED`` is a real value of the list, not a gap: saying
    the assessment has not been done is different from saying nothing.
    """

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    ASSESSMENT_NOT_PERFORMED = "Assessment not performed"


class FieldStatus(StrEnum):
    """Provenance status of a generated value (golden rule 1).

    Internal to the tool: never emitted to a filing. ``FILLED`` requires an
    authoritative source (GLEIF exact match, user overlay, a billing API);
    anything derived from the provider mapping or from state discovery is
    ``INFERRED``, never ``FILLED``.
    """

    FILLED = "FILLED"
    INFERRED = "INFERRED"
    MISSING = "MISSING"


class Priority(StrEnum):
    """Whether a missing field blocks a filing. Internal to the gap report."""

    BLOCKING = "BLOCKING"
    NON_BLOCKING = "NON_BLOCKING"
