"""The document an auditor asks for before they look at the register.

The gap report says what is missing. The inventory says what was read. Neither
says *how this was produced*, and that is the first question an examiner puts:
on what date, from which sources, by what method, and what did you decide not to
look at.

Generated from the same run as the register rather than written by hand, so it
cannot drift from the thing it describes. A methodology note that no longer
matches its register is worse than none — it documents a process that did not
happen.

One deliberate difference from every other output: this file carries a
timestamp. The machine-readable files omit one so a CI diff shows what changed
rather than that the clock moved; a register without a date tells an auditor
nothing about whether it describes today or last year.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from dora_roi.collectors.tfstate import NON_VENDOR_PROVIDERS
from dora_roi.models.enums import FieldStatus
from dora_roi.report.gap import GapEntry, summarize

__all__ = ["to_markdown"]

_METHOD = """\
Vendors were identified from three kinds of evidence, and nothing else:

1. **Terraform providers.** Each `provider[...]` block in a state file is a
   vendor relationship, weighted by how many resources run through it.
   Providers that generate a value locally and contract with nobody —
   {utilities} — are excluded.
2. **DNS records.** An MX record names who reads the mail, a CNAME who serves a
   subdomain, an SPF `include:` everyone allowed to send as the entity. These
   name vendors that appear in no provider block. Hostnames are matched against
   a packaged table on the registrable domain only, never as a substring.
3. **Kubernetes**, where a cluster was scanned: image registries, `ExternalName`
   services and ingress hosts.

Legal identity was resolved against GLEIF where enabled. A result counts as
confirmed only when the legal name GLEIF returned matches the name that was
searched; anything else is recorded as a candidate and marked inferred, because
GLEIF's name filter is a search and not an equality test.

Field codes, names and closed lists were reconciled against the official EBA
annotated table layout and sample report package, which are vendored in this
repository and re-checked by the test suite on every run.
"""


def _systemic_caveats(entries: Sequence[GapEntry]) -> list[tuple[str, str, str]]:
    """The notes that qualify a whole template, not a single vendor.

    Both arrive as an entry note, and only one belongs in an audit document. The
    difference is reach: "an AWS account is not a legal entity" is true of every
    row of B_01.02, while "static hosting and edge functions" describes one
    provider. Listing both would bury the first under the second, so a caveat
    qualifies only if it applies to every row its template has.
    """
    rows_per_template: dict[str, set[str]] = {}
    for entry in entries:
        if entry.row_key:
            rows_per_template.setdefault(entry.template, set()).add(entry.row_key)

    reach: dict[tuple[str, str, str], set[str]] = {}
    for entry in entries:
        if entry.status is FieldStatus.INFERRED and entry.note and entry.source and entry.row_key:
            reach.setdefault((entry.template, entry.source, entry.note), set()).add(entry.row_key)

    return sorted(key for key, rows in reach.items() if rows == rows_per_template.get(key[0], set()))


def to_markdown(
    entries: Sequence[GapEntry],
    perimeter: dict[str, Any],
    providers: Sequence[Any],
    excluded: Sequence[str] = (),
    *,
    generated_at: datetime | None = None,
    version: str = "",
) -> str:
    """The audit-facing account of how a register was produced."""
    summary = summarize(entries)
    when = (generated_at or datetime.now(tz=UTC)).strftime("%Y-%m-%d %H:%M UTC")

    out = [
        "# Register of Information — how this was produced",
        "",
        f"Generated **{when}** by dora-roi {version}.".strip(),
        "",
        "> This is a record of method and perimeter, not a compliance statement. The register "
        "it describes contains hypotheses; the column marked *inferred* below counts them.",
        "",
        "## What was read",
        "",
    ]

    state_files = perimeter.get("state_files") or []
    out.append(f"**{len(state_files)} Terraform state file(s):**")
    out.append("")
    out += [f"- `{path}`" for path in state_files] or ["- none"]
    out += [
        "",
        f"- **AWS Organizations and Cost Explorer:** {'read' if perimeter.get('aws') else 'not read'}",
        f"- **Kubernetes:** {perimeter['kubernetes'] if perimeter.get('kubernetes') else 'not read'}",
        f"- **GLEIF:** {'consulted' if perimeter.get('gleif') else 'not consulted'}",
        f"- **Manual overlay:** {perimeter.get('overlay') or 'none supplied'}",
        "",
        "## What was deliberately not read",
        "",
    ]
    out += [f"- {note}" for note in excluded] or [
        "- Nothing was in reach and excluded: every source named above was read in full."
    ]
    out += [
        "",
        "Beyond that, anything outside the sources above is invisible to this method: resources "
        "created by hand, SaaS bought on a card, and any contract with no infrastructure footprint "
        "at all. **This register does not claim to be complete.**",
        "",
        "## How vendors were identified",
        "",
        _METHOD.format(utilities=", ".join(f"`{name}`" for name in sorted(NON_VENDOR_PROVIDERS))),
        "## What the register asserts",
        "",
        f"{len(providers)} ICT third-party service provider(s) were identified across "
        f"{summary.total} fields in {len(summary.by_template)} templates.",
        "",
        "| Basis | Fields | What it means |",
        "|---|---:|---|",
        f"| Filled | {summary.filled} | An authoritative source said so: a GLEIF exact match, "
        f"a billing API, or a human assertion in the overlay. |",
        f"| Inferred | {summary.inferred} | Derived from a mapping or from infrastructure. "
        f"**Reviewed by a person before filing, or it is a guess.** |",
        f"| Missing | {summary.missing} | No source could produce a value. "
        f"{summary.blocking_missing} of these are mandatory and block a filing. |",
        "",
    ]

    if summary.unprovenanced:
        out += [
            f"⚠️ {summary.unprovenanced} field(s) hold a value with no recorded basis. That is a "
            f"defect in the tool and those values should not be relied on.",
            "",
        ]

    out += [
        "### By template",
        "",
        "| Template | Fields | Filled | Inferred | Missing | Blocking |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for template in sorted(summary.by_template):
        per = summary.by_template[template]
        out.append(
            f"| {template} | {per.total} | {per.filled} | {per.inferred} | {per.missing} | {per.blocking_missing} |"
        )

    caveats = _systemic_caveats(entries)
    if caveats:
        out += [
            "",
            "## What the inferred values actually mean",
            "",
            "Some values are qualified by the way they were derived, and the qualification "
            "matters more than the value. These apply to **every row** of their template:",
            "",
        ]
        for template, source, note in caveats:
            out.append(f"- **{template}**, from `{source}` — {note}")

    filled_sources = sorted({e.source for e in entries if e.status is FieldStatus.FILLED and e.source})
    out += [
        "",
        "## Evidence",
        "",
        "- `inventory.json` — every provider found, with the file each was found in.",
        "- `gap-report.md` / `.json` — every field, its basis, and whether it blocks a filing.",
        "- `roi_prefill.json` — the register itself, keyed by official field code, each value "
        "paired with where it came from.",
        "",
        f"Authoritative sources relied on: {', '.join(f'`{s}`' for s in filled_sources) or 'none'}.",
        "",
    ]
    return "\n".join(out)
