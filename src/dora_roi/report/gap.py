"""The gap report: what is filled, what was guessed, and what is still missing.

This is the output the tool exists for. A half-filled register is not useful on
its own — what is useful is knowing precisely which half is missing and which
part of that half stops a filing.

Three decisions shape it.

**Nothing is invisible.** The report walks ``FIELD_CATALOG``, not the models,
so the eleven templates with no row model yet still appear, as synthetic
all-MISSING entries. A template the tool cannot represent must not read as a
template with nothing wrong.

**Provenance is the truth, not the presence of a value.** A field is FILLED
because something authoritative said so, not because an attribute is non-empty.
That makes one failure mode possible — a value set by a pipeline that forgot to
mark it — so instead of letting it hide as an ordinary MISSING, entries carry
``unprovenanced`` and say so in the note. It is a bug in this tool, and it
should read like one.

**A conditional obligation is reported as blocking.** B_02.02.0150 is mandatory
only when data is stored, and nothing here can evaluate that for you. Reported
as BLOCKING with the condition spelled out: a false blocker costs a review, a
missed one costs a rejected filing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from dora_roi.models.enums import FieldStatus, Priority
from dora_roi.models.templates import (
    FIELD_CATALOG,
    MODELLED_TEMPLATES,
    TEMPLATE_COLLECTIONS,
    UNVERIFIED_FIELD_NAMES,
    RegisterOfInformation,
    RoIRow,
)

__all__ = [
    "MODEL_FIELD_MAP",
    "GapEntry",
    "GapSummary",
    "TemplateSummary",
    "build_gap_report",
    "summarize",
    "to_json",
    "to_markdown",
]

#: template -> {official code: python attribute}. Derived from the models'
#: aliases rather than hand-written, so it cannot drift from them.
MODEL_FIELD_MAP: dict[str, dict[str, str]] = {
    template: {info.alias: name for name, info in model.model_fields.items() if info.alias is not None}
    for template, model in MODELLED_TEMPLATES.items()
}

#: Which field identifies a row in a human-readable way, per template. Falls
#: back to a positional "#1" when the key field is itself still empty — which,
#: in a prefill, it very often is.
_ROW_KEY_FIELD = {
    "B_01.01": "name",
    "B_01.02": "name",
    "B_01.03": "name",
    "B_02.01": "arrangement_reference",
    "B_02.02": "arrangement_reference",
    "B_02.03": "arrangement_reference",
    "B_03.01": "arrangement_reference",
    "B_03.02": "arrangement_reference",
    "B_03.03": "arrangement_reference",
    "B_04.01": "arrangement_reference",
    "B_05.01": "legal_name",
    "B_05.02": "arrangement_reference",
    "B_06.01": "name",
    "B_07.01": "arrangement_reference",
    "B_99.01": "term",
}

_UNPROVENANCED_NOTE = (
    "a value is set on this field but nothing recorded its provenance, so it cannot be trusted; "
    "this is a bug in dora-roi, please report it"
)

_DISCLAIMER = (
    "dora-roi does not make you compliant and does not file anything. It produces hypotheses and a "
    "gap analysis; every INFERRED value needs review before it reaches a filing."
)


@dataclass(frozen=True)
class GapEntry:
    """One field of one row: where it stands and whether that blocks a filing."""

    template: str
    code: str
    name: str
    row_key: str | None
    status: FieldStatus
    source: str | None
    priority: Priority
    note: str | None
    unprovenanced: bool = False

    @property
    def is_blocking_gap(self) -> bool:
        return self.priority is Priority.BLOCKING and self.status is FieldStatus.MISSING


@dataclass
class TemplateSummary:
    """Counts for one template."""

    total: int = 0
    filled: int = 0
    inferred: int = 0
    missing: int = 0
    blocking_missing: int = 0
    modelled: bool = False


@dataclass
class GapSummary:
    """Counts for the whole register."""

    total: int = 0
    filled: int = 0
    inferred: int = 0
    missing: int = 0
    blocking_missing: int = 0
    unprovenanced: int = 0
    by_template: dict[str, TemplateSummary] = field(default_factory=dict)


def build_gap_report(roi: RegisterOfInformation) -> list[GapEntry]:
    """Walk the catalog against the register. Blocking gaps first."""
    rows_by_template = _rows_by_template(roi)
    entries: list[GapEntry] = []

    for template, fields in FIELD_CATALOG.items():
        rows = rows_by_template.get(template, [])
        if not rows:
            entries.extend(_synthetic(template, fields))
            continue
        by_code = MODEL_FIELD_MAP.get(template, {})
        for index, row in enumerate(rows, start=1):
            key = _row_key(template, row, index)
            for spec in fields:
                entries.append(_entry_for(template, spec, row, by_code, key))

    return sorted(entries, key=_ordering)


def summarize(entries: Sequence[GapEntry]) -> GapSummary:
    """Aggregate counts, overall and per template."""
    summary = GapSummary()
    for entry in entries:
        per = summary.by_template.setdefault(entry.template, TemplateSummary())
        per.modelled = entry.template in MODELLED_TEMPLATES
        per.total += 1
        summary.total += 1

        if entry.status is FieldStatus.FILLED:
            per.filled += 1
            summary.filled += 1
        elif entry.status is FieldStatus.INFERRED:
            per.inferred += 1
            summary.inferred += 1
        else:
            per.missing += 1
            summary.missing += 1

        if entry.is_blocking_gap:
            per.blocking_missing += 1
            summary.blocking_missing += 1
        if entry.unprovenanced:
            summary.unprovenanced += 1
    return summary


def to_markdown(entries: Sequence[GapEntry]) -> str:
    """Human-facing report: what stops a filing, then everything else."""
    summary = summarize(entries)
    out: list[str] = ["# DORA Register of Information — gap report", ""]
    out += [f"> {_DISCLAIMER}", ""]
    out += [
        f"**{summary.filled} filled · {summary.inferred} inferred · {summary.missing} missing** "
        f"across {summary.total} fields in {len(summary.by_template)} templates. "
        f"**{summary.blocking_missing} of the missing ones block a filing.**",
        "",
    ]
    if summary.unprovenanced:
        out += [
            f"⚠️ {summary.unprovenanced} field(s) hold a value with no recorded provenance. "
            f"That is a bug in dora-roi — please report it.",
            "",
        ]

    out += [
        "## Summary",
        "",
        "| Template | Fields | Filled | Inferred | Missing | Blocking | Modelled |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for template in sorted(summary.by_template):
        per = summary.by_template[template]
        flag = "yes" if per.modelled else "not yet"
        out.append(
            f"| {template} | {per.total} | {per.filled} | {per.inferred} | {per.missing} | "
            f"{per.blocking_missing} | {flag} |"
        )
    out.append("")

    blocking = [e for e in entries if e.is_blocking_gap]
    out += ["## Blocking gaps first", ""]
    if blocking:
        out += ["| Template | Code | Field | Row | Note |", "|---|---|---|---|---|"]
        out += [_row_md(e) for e in blocking]
    else:
        out.append("No blocking gaps. Every mandatory field has a value.")
    out.append("")

    rest = [e for e in entries if not e.is_blocking_gap and e.status is not FieldStatus.FILLED]
    out += ["## Everything else still open", ""]
    if rest:
        out += ["| Template | Code | Field | Row | Status | Source | Note |", "|---|---|---|---|---|---|---|"]
        out += [
            f"| {e.template} | {e.code} | {_escape(e.name)} | {_escape(e.row_key or '—')} | {e.status} | "
            f"{_escape(e.source or '—')} | {_escape(e.note or '')} |"
            for e in rest
        ]
    else:
        out.append("Nothing.")
    out.append("")

    if UNVERIFIED_FIELD_NAMES:
        listed = ", ".join(sorted(UNVERIFIED_FIELD_NAMES))
        out += [
            "## Field names still to verify",
            "",
            f"Field *names* for {listed} were derived from each template's role, not read from the "
            f"official EBA annotated template. Codes, counts and mandatory flags are as documented. "
            f"Treat the names as labels, not as citations.",
            "",
        ]
    return "\n".join(out)


def to_json(entries: Sequence[GapEntry]) -> str:
    """Machine-facing report, for CI and for a future `--fail-on blocking`."""
    payload: dict[str, Any] = {
        "disclaimer": _DISCLAIMER,
        "summary": _summary_as_dict(summarize(entries)),
        "entries": [_entry_as_dict(e) for e in entries],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


# -- internals --------------------------------------------------------------


def _rows_by_template(roi: RegisterOfInformation) -> dict[str, list[RoIRow]]:
    """Read the register through TEMPLATE_COLLECTIONS rather than by hand.

    This was four hard-coded branches once. With fifteen templates that
    would be fifteen chances to forget one, and a forgotten template reports as
    entirely missing while quietly holding data.
    """
    rows: dict[str, list[RoIRow]] = {}
    for template, attribute in TEMPLATE_COLLECTIONS.items():
        held = getattr(roi, attribute, None)
        if held is None:
            continue
        rows[template] = list(held) if isinstance(held, list) else [held]
    return rows


def _synthetic(template: str, fields: Sequence[Any]) -> list[GapEntry]:
    """A template with no rows: every field missing, nothing hidden."""
    return [
        GapEntry(
            template=template,
            code=spec.code,
            name=spec.name,
            row_key=None,
            status=FieldStatus.MISSING,
            source=None,
            priority=_priority(spec),
            note=_note_for(spec, None),
        )
        for spec in fields
    ]


def _entry_for(template: str, spec: Any, row: RoIRow, by_code: dict[str, str], row_key: str) -> GapEntry:
    attribute = by_code.get(spec.code)
    status = FieldStatus.MISSING
    source: str | None = None
    provenance_note: str | None = None
    unprovenanced = False

    if attribute is not None:
        recorded = row.provenance.get(attribute)
        if recorded is not None:
            status = recorded.status
            source = recorded.source
            provenance_note = recorded.note
        elif getattr(row, attribute, None) is not None:
            unprovenanced = True

    return GapEntry(
        template=template,
        code=spec.code,
        name=spec.name,
        row_key=row_key,
        status=status,
        source=source,
        priority=_priority(spec),
        note=_note_for(spec, provenance_note, unprovenanced),
        unprovenanced=unprovenanced,
    )


def _priority(spec: Any) -> Priority:
    return Priority.BLOCKING if spec.mandatory else Priority.NON_BLOCKING


def _note_for(spec: Any, provenance_note: str | None, unprovenanced: bool = False) -> str | None:
    parts = [part for part in (spec.condition, provenance_note) if part]
    if unprovenanced:
        parts.append(_UNPROVENANCED_NOTE)
    return "; ".join(parts) or None


def _row_key(template: str, row: RoIRow, index: int) -> str:
    attribute = _ROW_KEY_FIELD.get(template)
    value = getattr(row, attribute, None) if attribute else None
    return str(value) if value else f"#{index}"


def _ordering(entry: GapEntry) -> tuple[int, int, str, str, str]:
    """Blocking-and-missing first, then missing, then template and code."""
    return (
        0 if entry.is_blocking_gap else 1,
        0 if entry.status is FieldStatus.MISSING else 1,
        entry.template,
        entry.code,
        entry.row_key or "",
    )


def _row_md(entry: GapEntry) -> str:
    return (
        f"| {entry.template} | {entry.code} | {_escape(entry.name)} | "
        f"{_escape(entry.row_key or '—')} | {_escape(entry.note or '')} |"
    )


def _escape(text: str) -> str:
    """Keep a pipe in a field name from breaking the table."""
    return text.replace("|", "\\|")


def _entry_as_dict(entry: GapEntry) -> dict[str, Any]:
    data = asdict(entry)
    data["status"] = str(entry.status)
    data["priority"] = str(entry.priority)
    return data


def _summary_as_dict(summary: GapSummary) -> dict[str, Any]:
    data = asdict(summary)
    data["by_template"] = {name: asdict(per) for name, per in sorted(summary.by_template.items())}
    return data
