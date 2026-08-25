"""The manual overlay: the facts no scanner can produce.

Everything the collectors and GLEIF can reach is, at best, infrastructure
evidence. A contract reference, a notice period, a governing law, whether an
exit plan exists — none of it is anywhere in a cluster or a state file, and
none of it ever will be. It comes from procurement, legal and the risk
function, and this module is where a human writes it down.

Two properties make the overlay trustworthy.

**It wins, and it means it.** An overlay value overwrites whatever enrichment
produced and flips the field to ``FILLED, source="overlay"``. That is the only
place in the tool, besides an exact GLEIF match, where a value earns FILLED —
and it earns it because a person asserted it.

**A typo is an error, never a shrug.** Every model forbids unknown keys. A
silently ignored ``legal_nmae`` would leave the user certain they had filled a
field the report still counts as missing, which is worse than never having
offered the overlay at all.

Fields whose template has no row model yet are accepted, validated, and
reported as parked rather than quietly dropped — see :class:`ContractOverlay`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from dora_roi.models.enums import (
    ContractualArrangementType,
    DataSensitiveness,
    FieldStatus,
    LevelOfReliance,
    TypeOfPerson,
)
from dora_roi.models.templates import (
    LEI_PATTERN,
    ContractualArrangementSpecific,
    CountryCode,
    CurrencyCode,
    EntityMaintainingRegister,
    Lei,
    Provenance,
    RegisterOfInformation,
    RoIRow,
)

__all__ = [
    "ArrangementOverlay",
    "ContractOverlay",
    "EntityOverlay",
    "Overlay",
    "CONTRACT_FIELD_TO_ROW",
    "PROVIDER_FIELD_TO_ROW",
    "OverlayError",
    "ProviderOverlay",
    "apply_overlay",
    "load_overlay",
    "overlay_template",
]

import re

_SOURCE = "overlay"
_LEI_RE = re.compile(LEI_PATTERN)


class OverlayError(Exception):
    """The overlay could not be read, or says something impossible."""


class _Strict(BaseModel):
    """Unknown keys are errors everywhere in this file. See the module docstring."""

    model_config = ConfigDict(extra="forbid")


class EntityOverlay(_Strict):
    """B_01.01 — who is filing. Nothing here is discoverable from infrastructure."""

    lei: Lei | None = None
    name: str | None = None
    country: CountryCode | None = None
    entity_type: str | None = None
    competent_authority: str | None = None
    reporting_date: date | None = None


class ArrangementOverlay(_Strict):
    """B_02.01 — the contractual arrangement, generally."""

    reference: str | None = None
    type: ContractualArrangementType | None = None
    overarching_reference: str | None = None
    currency: CurrencyCode | None = None
    annual_expense: Decimal | None = None


class ContractOverlay(_Strict):
    """B_02.02 — contract detail. Accepted and validated now, applied in A5.

    These fields have no row model yet. They are parsed and type-checked here
    rather than rejected, so a user can write the whole overlay once; what they
    are *not* is silently swallowed — :func:`apply_overlay` reports every parked
    block by name.
    """

    start_date: date | None = None
    end_date: date | None = None
    termination_reason: str | None = None
    notice_period_entity_days: int | None = None
    notice_period_provider_days: int | None = None
    governing_law_country: CountryCode | None = None
    country_of_provision: CountryCode | None = None
    storage_of_data: bool | None = None
    location_of_data_at_rest: CountryCode | None = None
    location_of_data_management: CountryCode | None = None
    data_sensitiveness: DataSensitiveness | None = None
    level_of_reliance: LevelOfReliance | None = None
    function_identifier: str | None = None


class ProviderOverlay(_Strict):
    """B_05.01 — the provider, as your contracts and registries actually name it."""

    identification_code: str | None = None
    type_of_code: str | None = None
    additional_code: str | None = None
    type_of_additional_code: str | None = None
    legal_name: str | None = None
    name_latin: str | None = None
    person_type: TypeOfPerson | None = None
    hq_country: CountryCode | None = None
    currency: CurrencyCode | None = None
    total_annual_expense: Decimal | None = None
    ultimate_parent_code: str | None = None
    type_of_parent_code: str | None = None

    arrangement: ArrangementOverlay | None = None
    contract: ContractOverlay | None = None

    @model_validator(mode="after")
    def _codes_must_match_their_declared_type(self) -> ProviderOverlay:
        """A code typed LEI has to be one, checked here and not only at the row.

        This is the single highest-risk value a human types into this file:
        invalid LEIs were about a third of the 2025 first-filing failures, and
        an overlay value is asserted as FILLED — the status that stops anything
        downstream from questioning it.
        """
        for code_field, type_field in (
            ("identification_code", "type_of_code"),
            ("ultimate_parent_code", "type_of_parent_code"),
        ):
            code = getattr(self, code_field)
            if code is not None and getattr(self, type_field) == "LEI" and not _LEI_RE.fullmatch(code):
                raise ValueError(f"{code_field} is typed LEI but {code!r} is not a valid LEI (ISO 17442)")
        return self


#: Arrangement overlay field -> B_02.01 attribute. `type` and `reference` are
#: shorter in YAML than in the model, and the mapping is where that is stated
#: once instead of guessed at the call site.
ARRANGEMENT_FIELD_TO_ROW: dict[str, str] = {
    "reference": "arrangement_reference",
    "type": "arrangement_type",
    "overarching_reference": "overarching_reference",
    "currency": "currency",
    "annual_expense": "annual_expense",
}

#: Contract overlay field -> B_02.02 attribute. The YAML says "…_days" because
#: a notice period is a number of days and the unit belongs in the name someone
#: types; the template's own field is just the integer.
CONTRACT_FIELD_TO_ROW: dict[str, str] = {
    "start_date": "start_date",
    "end_date": "end_date",
    "termination_reason": "termination_reason",
    "notice_period_entity_days": "notice_period_entity",
    "notice_period_provider_days": "notice_period_provider",
    "governing_law_country": "governing_law_country",
    "country_of_provision": "country_of_provision",
    "storage_of_data": "storage_of_data",
    "location_of_data_at_rest": "location_of_data_at_rest",
    "location_of_data_management": "location_of_data_management",
    "data_sensitiveness": "data_sensitiveness",
    "level_of_reliance": "level_of_reliance",
    "function_identifier": "function_identifier",
}

#: Overlay field -> row attribute, for the fields that have a row model today.
#: Module level, not a class attribute: a leading underscore inside a pydantic
#: model is a private attr, and a plain name would be taken for a field.
PROVIDER_FIELD_TO_ROW: dict[str, str] = {
    "identification_code": "identification_code",
    "type_of_code": "type_of_code",
    "additional_code": "additional_code",
    "type_of_additional_code": "type_of_additional_code",
    "legal_name": "legal_name",
    "name_latin": "name_latin",
    "person_type": "person_type",
    "hq_country": "headquarters_country",
    "currency": "currency",
    "total_annual_expense": "total_annual_expense",
    "ultimate_parent_code": "ultimate_parent_code",
    "type_of_parent_code": "type_of_parent_code",
}


class Overlay(_Strict):
    """A whole ``vendors.yaml``."""

    entity: EntityOverlay | None = None
    providers: dict[str, ProviderOverlay] = {}

    @field_validator("providers", mode="before")
    @classmethod
    def _empty_sections_are_empty(cls, value: Any) -> Any:
        """An empty section means "nothing asserted", not a type error.

        Both `providers:` with every block commented out and `aws:` with every
        field commented out parse as None. That is exactly what `overlay init`
        writes and what a careful user leaves behind.
        """
        if value is None:
            return {}
        if isinstance(value, dict):
            return {key: ({} if entry is None else entry) for key, entry in value.items()}
        return value


def load_overlay(path: str | Path) -> Overlay:
    """Read and validate ``vendors.yaml``. Unknown keys are errors."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise OverlayError(f"overlay file not found: {path}") from e
    except OSError as e:
        raise OverlayError(f"could not read overlay file {path}: {e}") from e

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise OverlayError(f"{path} is not valid YAML: {e}") from e

    if document is None:
        return Overlay()
    if not isinstance(document, dict):
        raise OverlayError(f"{path} must be a mapping with `entity` and `providers`, got {type(document).__name__}.")

    try:
        return Overlay(**document)
    except ValidationError as e:
        raise OverlayError(f"{path}: {_explain(e)}") from e


def apply_overlay(roi: RegisterOfInformation, overlay: Overlay) -> list[str]:
    """Write the overlay over the register. Returns what could not be applied.

    Rows are found by ``source_key`` — the Terraform provider name each was
    built from. An earlier version matched by list position, which was correct
    only for as long as `scan` stayed the single caller and kept building
    providers, arrangements and links in one aligned pass. The first collector
    to emit rows in a different order would have written every overlay block
    onto the wrong provider, silently and plausibly.

    Anything that could not be applied comes back as a warning rather than
    disappearing: an overlay key matching no provider is almost always a typo,
    and a user who thinks they filled a field is worse off than one who knows
    they did not.
    """
    warnings: list[str] = []

    if overlay.entity is not None:
        roi.entity = roi.entity or EntityMaintainingRegister()
        for field, value in overlay.entity.model_dump(exclude_none=True).items():
            _set(roi.entity, field, value)

    providers = {row.source_key: row for row in roi.providers if row.source_key}
    arrangements = {row.source_key: row for row in roi.arrangements if row.source_key}

    for key, entry in overlay.providers.items():
        row = providers.get(key)
        if row is None:
            warnings.append(
                f"overlay: no provider named {key!r} was discovered, so its block was not applied. "
                f"Discovered: {', '.join(sorted(providers)) or 'none'}."
            )
            continue

        for field, attribute in PROVIDER_FIELD_TO_ROW.items():
            value = getattr(entry, field)
            if value is not None:
                _set(row, attribute, value)

        arrangement = arrangements.get(key)
        if entry.arrangement is not None and arrangement is not None:
            for field, value in entry.arrangement.model_dump(exclude_none=True).items():
                _set(arrangement, ARRANGEMENT_FIELD_TO_ROW[field], value)

        contract = entry.contract.model_dump(exclude_none=True) if entry.contract else {}
        if contract:
            roi.arrangement_details.append(_contract_row(key, arrangement, row, contract))

    return warnings


def _contract_row(
    key: str, arrangement: RoIRow | None, provider: RoIRow, values: dict[str, Any]
) -> ContractualArrangementSpecific:
    """Build the B_02.02 row a `contract:` block describes, joined to its arrangement."""
    detail = ContractualArrangementSpecific(source_key=key)

    reference = getattr(arrangement, "arrangement_reference", None) if arrangement is not None else None
    if reference and arrangement is not None:
        # A join, not an assertion: the reference is FILLED only where the
        # arrangement it came from is.
        detail.arrangement_reference = reference
        detail.provenance["arrangement_reference"] = arrangement.provenance.get("arrangement_reference") or Provenance(
            status=FieldStatus.INFERRED, source="dora-roi", note="joined from B_02.01"
        )

    code = getattr(provider, "identification_code", None)
    if code:
        detail.provider_code = code
        detail.provenance["provider_code"] = provider.provenance.get("identification_code") or Provenance(
            status=FieldStatus.INFERRED, source="dora-roi", note="joined from B_05.01"
        )

    for field, value in values.items():
        _set(detail, CONTRACT_FIELD_TO_ROW[field], value)
    return detail


def overlay_template(provider_names: list[str], resolved: Mapping[str, Mapping[str, str]] | None = None) -> str:
    """A commented ``vendors.yaml``, seeded with the providers a scan found.

    Everything is commented out on purpose. An uncommented placeholder would be
    asserted as FILLED the moment the file is used, which is the one thing this
    file must never do by accident.

    ``resolved`` maps a provider to the fields a scan already settled and how.
    Ten identical blocks tell you nothing about where to spend an afternoon; a
    block that says the LEI is already confirmed, and the one next to it that
    says it is not, tells you exactly.
    """
    resolved = resolved or {}
    lines = [
        "# vendors.yaml — the facts no scanner can produce.",
        "#",
        "# Every value you uncomment here is recorded as FILLED, source=overlay, and",
        "# overwrites whatever the scan inferred. That is the point: you are asserting",
        "# it. Leave anything you do not know commented out — the gap report is more",
        "# useful telling you a field is missing than repeating a guess back to you.",
        "#",
        "# Unknown keys are an error, not a shrug, so a typo fails loudly instead of",
        "# leaving you certain you filled a field that is still empty.",
        "",
        "# entity:",
        "#   lei: 529900T8BM49AURSDO55        # your LEI, validated against GLEIF",
        "#   name: Acme Payments SpA",
        "#   country: IT",
        "#   entity_type: payment institution   # an official value: see `dora-roi check`",
        "#   competent_authority: Banca d'Italia",
        "#   reporting_date: 2026-03-31       # one date for the whole filing",
        "",
        "providers:",
    ]
    if not provider_names:
        lines += [
            "  # No providers yet — run `dora-roi scan` first and re-run",
            "  # `dora-roi overlay init` to get a block per discovered provider.",
            "  {}",
        ]
    for name in provider_names:
        lines += _provider_block(name, resolved.get(name, {}))
    return "\n".join(lines) + "\n"


def _provider_block(name: str, known: Mapping[str, str]) -> list[str]:
    """One provider, annotated with what the scan already settled."""
    lines = [f"  {name}:"]
    settled = {field: value for field, value in known.items() if value}
    for field, value in sorted(settled.items()):
        lines.append(f"    # {field} is already confirmed: {value}. Override only if your contract disagrees.")

    if "identification_code" not in settled:
        lines += [
            "    # ⚠ no identification code was resolved for this provider — a blocking gap.",
            "    #   Look it up at search.gleif.org, or use the national code from the contract",
            "    #   (LU_CRN, IT_VAT, …) if the entity has no LEI.",
            "    # identification_code: 5493001KJTIIGC8Y1R12",
            "    # type_of_code: LEI",
        ]
    if "legal_name" not in settled:
        lines.append("    # legal_name: the entity named on your contract")
    if "headquarters_country" not in settled:
        lines.append("    # hq_country: LU")
    lines += [
        "    # ultimate_parent_code: 5493001KJTIIGC8Y1R12",
        "    # type_of_parent_code: LEI",
        "    # arrangement:                   # B_02.01 — every field here is blocking",
        "    #   # A reference YOU assign. Click-through cloud contracts have no",
        "    #   # vendor-issued number: use the account ID, a procurement ticket,",
        "    #   # a row in your contract register. Unique, and stable across filings.",
        "    #   reference: CTR-2024-0917",
        "    #   type: Standalone arrangement",
        "    #   currency: EUR",
        "    #   annual_expense: 120000",
        "    # contract:                      # B_02.02",
        "    #   start_date: 2024-01-01",
        "    #   notice_period_entity_days: 90",
        "    #   governing_law_country: IT",
        "    #   level_of_reliance: Material reliance",
        "",
    ]
    return lines


def _set(row: RoIRow, attribute: str, value: Any) -> None:
    """Assign and mark FILLED. Assignment is validated (see templates.py)."""
    try:
        setattr(row, attribute, value)
    except ValidationError as e:
        raise OverlayError(f"overlay value for {attribute!r} is invalid: {_explain(e)}") from e
    row.mark(attribute, FieldStatus.FILLED, source=_SOURCE)


def _explain(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        location = ".".join(str(piece) for piece in item["loc"]) or "document"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)
