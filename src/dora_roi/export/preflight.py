"""Pre-flight: the checks that stand between a prefill and a rejected filing.

The gap report answers "what is missing?". This answers a narrower and harsher
question: "if you filed this today, what would come back?"

The checks are not invented. Each one encodes a failure mode observed in the
2025 first filing: missing or invalid LEIs, contracts
aggregated instead of modelled one per arrangement, empty sub-outsourcing for
hyperscalers, missing exit plans, reference dates that disagree across
templates. Together those accounted for most of what got rejected.

Two severities, and the line between them is deliberate. BLOCKING means a
filing built from this register is expected to be refused. WARNING means it
would be accepted and would still be wrong — an unreviewed INFERRED value is
the clearest case: nothing rejects it, and nobody but you knows it was a guess.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from dora_roi.models.enums import FieldStatus
from dora_roi.models.templates import (
    FIELD_CATALOG,
    LEI_PATTERN,
    MODELLED_TEMPLATES,
    TEMPLATE_COLLECTIONS,
    UNVERIFIED_FIELD_NAMES,
    Provenance,
    RegisterOfInformation,
    RoIRow,
)

__all__ = [
    "Finding",
    "PreflightError",
    "Severity",
    "load_prefill",
    "preflight",
    "summarise_findings",
]

_LEI_RE = re.compile(LEI_PATTERN)


@cache
def _eba_required() -> dict[str, frozenset[str]]:
    """template -> columns an ACTIVE EBA validation rule requires to be non-null.

    Derived from the official validation rules workbook, filtered to
    ``status == active``: thirteen of the seventy-one DORA rules are deactivated,
    and the workbook lists them all together. Reading it without that filter
    would enforce rules the EBA has withdrawn.

    These are *not* folded into FieldSpec.mandatory, for two reasons. The EBA
    grades every one of them `warning`, not `error`. And several read oddly as
    absolute obligations — "Date of deletion in the Register of information" is
    required by rule, yet an entity that has not been deleted has no date to give.
    Treating them as blocking would invent failures; ignoring them would hide the
    single largest category of real ones, since missing mandatory data was 86% of
    the dry-run errors. So they get their own finding, at the EBA's own severity.
    """
    raw = resources.files("dora_roi.data").joinpath("eba_closed_lists.json").read_text(encoding="utf-8")
    entry = json.loads(raw)["_eba_required_columns"]
    return {template: frozenset(columns) for template, columns in entry["columns"].items()}


#: Providers whose own supply chain is never one link long. Sub-outsourcing left
#: empty for one of these is a known first-filing failure, and it is spotted at once.
_HYPERSCALER_MARKERS = ("amazon web services", "google cloud", "microsoft", "oracle cloud", "ibm cloud")

#: The shape `scan` generates when nobody has supplied a real contract reference.
_SYNTHETIC_REFERENCE_RE = re.compile(r"^ARR-[A-Z0-9_]+-\d{3}$")


class PreflightError(Exception):
    """A prefill could not be read."""


class Severity(StrEnum):
    """BLOCKING: expect a rejection. WARNING: expect acceptance, and be wrong anyway."""

    BLOCKING = "BLOCKING"
    WARNING = "WARNING"


class _LeiLookup(Protocol):
    def lookup_by_lei(self, lei: str) -> Any: ...


@dataclass(frozen=True)
class Finding:
    """One thing wrong, and what to do about it."""

    code: str
    severity: Severity
    message: str
    fix: str
    template: str = ""
    field: str = ""
    row_key: str = ""


def preflight(roi: RegisterOfInformation, *, gleif: _LeiLookup | None = None) -> list[Finding]:
    """Every check, blocking findings first.

    ``gleif`` is optional and only used to confirm that an LEI exists and is
    ACTIVE. Without it the LEI checks stay offline and cover format and
    presence, which is most of the failure mode already.
    """
    findings: list[Finding] = []
    findings += _check_completeness(roi)
    findings += _check_leis(roi, gleif)
    findings += _check_reporting_date(roi)
    findings += _check_supply_chain(roi)
    findings += _check_arrangements(roi)
    findings += _check_duplicate_codes(roi)
    findings += _check_eba_rules(roi)
    findings += _check_domain_data()
    return sorted(findings, key=lambda f: (0 if f.severity is Severity.BLOCKING else 1, f.template, f.field))


def summarise_findings(findings: Sequence[Finding]) -> dict[str, int]:
    blocking = sum(1 for f in findings if f.severity is Severity.BLOCKING)
    return {"total": len(findings), "blocking": blocking, "warning": len(findings) - blocking}


def load_prefill(path: str | Path) -> RegisterOfInformation:
    """Rebuild a register from a ``roi_prefill.json``, hand-edits and all.

    The point of checking a file rather than re-running a scan: between the two,
    a person has been editing it. Values keyed by official code go straight back
    into the models, so an edit that breaks a validator is caught here rather
    than by a regulator.
    """
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise PreflightError(f"prefill not found: {path}. Run `dora-roi scan -o <dir>` first.") from e
    except json.JSONDecodeError as e:
        raise PreflightError(f"{path} is not valid JSON: {e}") from e

    templates = payload.get("templates") or {}
    if not isinstance(templates, dict):
        raise PreflightError(f"{path}: `templates` must be an object.")

    roi = RegisterOfInformation()
    for template, rows in templates.items():
        model = MODELLED_TEMPLATES.get(template)
        if model is None:
            raise PreflightError(f"{path}: unknown template {template!r}.")
        attribute = TEMPLATE_COLLECTIONS[template]
        built = [_build_row(model, entry, template, path) for entry in rows or []]
        if attribute == "entity":
            roi.entity = built[0] if built else None  # type: ignore[assignment]
        else:
            getattr(roi, attribute).extend(built)
    return roi


def _build_row(model: type[RoIRow], entry: Any, template: str, path: Path) -> RoIRow:
    if not isinstance(entry, dict):
        raise PreflightError(f"{path}: each row of {template} must be an object.")
    try:
        row = model(**{k: v for k, v in (entry.get("values") or {}).items() if v is not None})
    except ValidationError as e:
        raise PreflightError(f"{path}: {template} has an invalid value: {e.errors()[0]['msg']}") from e

    by_code = {info.alias: name for name, info in model.model_fields.items() if info.alias is not None}
    for code, recorded in (entry.get("provenance") or {}).items():
        name = by_code.get(code)
        if name and isinstance(recorded, dict):
            row.provenance[name] = Provenance(
                status=FieldStatus(recorded.get("status", "MISSING")),
                source=recorded.get("source"),
                note=recorded.get("note"),
            )
    return row


# -- the checks -------------------------------------------------------------


def _check_completeness(roi: RegisterOfInformation) -> list[Finding]:
    """Mandatory fields that are missing, and INFERRED ones nobody has reviewed."""
    findings: list[Finding] = []
    for template, rows in _rows(roi).items():
        by_code = {
            info.alias: name
            for name, info in MODELLED_TEMPLATES[template].model_fields.items()
            if info.alias is not None
        }
        for index, row in enumerate(rows, start=1):
            key = _row_key(row, index)
            for spec in FIELD_CATALOG[template]:
                status = row.status_of(by_code[spec.code])
                if spec.mandatory and status is FieldStatus.MISSING:
                    findings.append(
                        Finding(
                            code="MANDATORY_MISSING",
                            severity=Severity.BLOCKING,
                            message=f"{template}.{spec.code} ({spec.name}) is mandatory and has no value.",
                            fix="Supply it through the overlay, or remove the row if the relationship is not real."
                            + (f" Conditional: {spec.condition}." if spec.condition else ""),
                            template=template,
                            field=spec.code,
                            row_key=key,
                        )
                    )
                elif status is FieldStatus.INFERRED:
                    findings.append(
                        Finding(
                            code="UNREVIEWED_INFERRED",
                            severity=Severity.WARNING,
                            message=f"{template}.{spec.code} ({spec.name}) holds a guess, not a confirmed value.",
                            fix="Confirm it and assert it in the overlay, which records it as FILLED.",
                            template=template,
                            field=spec.code,
                            row_key=key,
                        )
                    )
    return findings


def _check_leis(roi: RegisterOfInformation, gleif: _LeiLookup | None) -> list[Finding]:
    """Missing and invalid LEIs were about a third of the first-filing failures."""
    findings: list[Finding] = []
    for index, provider in enumerate(roi.providers, start=1):
        key = _row_key(provider, index)
        code = provider.identification_code
        if not code:
            findings.append(
                Finding(
                    code="LEI_MISSING",
                    severity=Severity.BLOCKING,
                    message=f"provider {key!r} has no identification code.",
                    fix="Run the scan with --gleif, or set identification_code in the overlay.",
                    template="B_05.01",
                    field="0010",
                    row_key=key,
                )
            )
            continue
        if provider.type_of_code == "LEI" and not _LEI_RE.fullmatch(code):
            findings.append(
                Finding(
                    code="LEI_MALFORMED",
                    severity=Severity.BLOCKING,
                    message=f"provider {key!r} declares an LEI but {code!r} is not one (ISO 17442).",
                    fix="An LEI is 18 alphanumerics plus 2 check digits. Look it up at search.gleif.org.",
                    template="B_05.01",
                    field="0010",
                    row_key=key,
                )
            )
            continue
        if gleif is not None and provider.type_of_code == "LEI":
            findings += _check_one_lei_against_gleif(gleif, code, key)
    return findings


def _check_one_lei_against_gleif(gleif: _LeiLookup, code: str, key: str) -> list[Finding]:
    record = gleif.lookup_by_lei(code)
    if record is None:
        return [
            Finding(
                code="LEI_UNKNOWN_TO_GLEIF",
                severity=Severity.BLOCKING,
                message=f"LEI {code} for {key!r} is well formed but GLEIF does not know it.",
                fix="Check for a transcription error; the business-rule layer validates LEIs against GLEIF.",
                template="B_05.01",
                field="0010",
                row_key=key,
            )
        ]
    if getattr(record, "status", None) != "ACTIVE":
        return [
            Finding(
                code="LEI_NOT_ACTIVE",
                severity=Severity.BLOCKING,
                message=f"LEI {code} for {key!r} is {getattr(record, 'status', 'unknown')}, not ACTIVE.",
                fix="A lapsed LEI fails validation. Ask the provider to renew it, or use the current entity's LEI.",
                template="B_05.01",
                field="0010",
                row_key=key,
            )
        ]
    return []


def _check_reporting_date(roi: RegisterOfInformation) -> list[Finding]:
    """One reference date, propagated, or the templates disagree with each other."""
    if roi.entity is not None and roi.entity.reporting_date is not None:
        return []
    return [
        Finding(
            code="REPORTING_DATE_MISSING",
            severity=Severity.BLOCKING,
            message="the register has no reporting reference date (B_01.01.0060).",
            fix="Set entity.reporting_date in the overlay. One date for the whole filing, propagated everywhere.",
            template="B_01.01",
            field="0060",
        )
    ]


def _check_supply_chain(roi: RegisterOfInformation) -> list[Finding]:
    """Empty sub-outsourcing for a hyperscaler is spotted at once."""
    ranks = {link.rank for link in roi.supply_chain if link.rank is not None}
    if not ranks or max(ranks) > 1:
        return []
    findings = []
    for index, provider in enumerate(roi.providers, start=1):
        name = (provider.legal_name or "").lower()
        if any(marker in name for marker in _HYPERSCALER_MARKERS):
            key = _row_key(provider, index)
            findings.append(
                Finding(
                    code="SUPPLY_CHAIN_RANK1_ONLY",
                    severity=Severity.WARNING,
                    message=f"{key!r} is a hyperscaler and its supply chain stops at rank 1.",
                    fix="Ask the provider for its sub-outsourcing register and add the rank-2 links. "
                    "Leaving B_05.02 at rank 1 for a large cloud provider is a known review trigger.",
                    template="B_05.02",
                    field="0050",
                    row_key=key,
                )
            )
    return findings


def _check_arrangements(roi: RegisterOfInformation) -> list[Finding]:
    """Contracts aggregated instead of modelled one per arrangement — plus the
    references this tool invented and nobody replaced.
    """
    findings: list[Finding] = []
    seen: set[str] = set()
    for index, arrangement in enumerate(roi.arrangements, start=1):
        reference = arrangement.arrangement_reference
        if not reference:
            continue
        if reference in seen:
            findings.append(
                Finding(
                    code="ARRANGEMENT_REFERENCE_DUPLICATE",
                    severity=Severity.BLOCKING,
                    message=f"arrangement reference {reference!r} is used by more than one row.",
                    fix="One record per arrangement, not one per provider. Aggregating them is a known failure.",
                    template="B_02.01",
                    field="0010",
                    row_key=reference,
                )
            )
        seen.add(reference)
        if _SYNTHETIC_REFERENCE_RE.match(reference):
            findings.append(
                Finding(
                    code="SYNTHETIC_REFERENCE",
                    severity=Severity.BLOCKING,
                    message=f"{reference!r} was generated by dora-roi, so it is not yours to file.",
                    fix=(
                        "B_02.01.0010 is a reference *you* assign — most cloud contracts are "
                        "click-through and have no vendor-issued number, so there is nothing to look up. "
                        "Use something stable in your own systems: the AWS account ID, a procurement "
                        "ticket, a row in your contract register. It only has to be unique and to stay "
                        "the same across filings, because every other template joins on it. The generated "
                        "one is refused because it is derived from the Terraform provider name and moves "
                        "when that does. Set it via the overlay's `arrangement.reference`."
                    ),
                    template="B_02.01",
                    field="0010",
                    row_key=reference,
                )
            )
        _ = index
    return findings


def _check_duplicate_codes(roi: RegisterOfInformation) -> list[Finding]:
    """Two providers sharing an identification code are one provider, or a bug.

    An LEI identifies exactly one legal entity, so a register naming three
    vendors with the same one is wrong however it got there. It got there, in a
    real run, because GLEIF's name search answers "Auth0, Inc.", "Netlify, Inc."
    and "Webflow, Inc." with the same Canadian company — matching on the word
    "Inc.". The lookup was tightened; this catches the next thing that does it.
    """
    seen: dict[str, list[str]] = {}
    for index, provider in enumerate(roi.providers, start=1):
        if provider.identification_code:
            seen.setdefault(provider.identification_code, []).append(_row_key(provider, index))

    return [
        Finding(
            code="IDENTIFICATION_CODE_REUSED",
            severity=Severity.BLOCKING,
            message=f"{len(names)} providers share the identification code {code}: {', '.join(names)}.",
            fix="An identification code names one legal entity. Re-check them at search.gleif.org and "
            "set the right one for each in the overlay.",
            template="B_05.01",
            field="0010",
            row_key=names[0],
        )
        for code, names in sorted(seen.items())
        if len(names) > 1
    ]


def _check_eba_rules(roi: RegisterOfInformation) -> list[Finding]:
    """Columns an active EBA validation rule requires, that this register leaves empty."""
    findings: list[Finding] = []
    for template, rows in _rows(roi).items():
        required = _eba_required().get(template)
        if not required:
            continue
        names = {s.code: s.name for s in FIELD_CATALOG[template]}
        by_code = {
            info.alias: name
            for name, info in MODELLED_TEMPLATES[template].model_fields.items()
            if info.alias is not None
        }
        for index, row in enumerate(rows, start=1):
            key = _row_key(row, index)
            for code in sorted(required):
                if code in by_code and row.status_of(by_code[code]) is FieldStatus.MISSING:
                    findings.append(
                        Finding(
                            code="EBA_RULE_NULL",
                            severity=Severity.WARNING,
                            message=(
                                f"{template}.{code} ({names.get(code, code)}) is empty, and an active EBA "
                                f"validation rule requires it to be filled."
                            ),
                            fix="The EBA grades this rule `warning`, so it will not reject the filing on its "
                            "own — but missing mandatory data was 86% of the errors in the dry run.",
                            template=template,
                            field=code,
                            row_key=key,
                        )
                    )
    return findings


def _check_domain_data() -> list[Finding]:
    """This tool's own caveat, surfaced where somebody about to file will read it."""
    return [
        Finding(
            code="DOMAIN_DATA_UNVERIFIED",
            severity=Severity.WARNING,
            message=(
                f"{', '.join(sorted(UNVERIFIED_FIELD_NAMES))} is not reconciled against the official EBA "
                f"annotated template: the drafting notes describe three definition fields where the EBA layout has "
                f"nineteen closed-list columns."
            ),
            fix="Do not rely on that template's output. Every other template was reconciled.",
        )
    ]


def _rows(roi: RegisterOfInformation) -> dict[str, list[RoIRow]]:
    rows: dict[str, list[RoIRow]] = {}
    for template, attribute in TEMPLATE_COLLECTIONS.items():
        held = getattr(roi, attribute, None)
        if held is None:
            continue
        found = list(held) if isinstance(held, list) else [held]
        if found:
            rows[template] = found
    return rows


def _row_key(row: RoIRow, index: int) -> str:
    for candidate in ("legal_name", "arrangement_reference", "name", "term", "branch_code"):
        value = getattr(row, candidate, None)
        if value:
            return str(value)
    return f"#{index}"
