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


#: Markers `_collect_aws` (cli.py) writes into `clickops_refused`, spelled out
#: here rather than derived, for the same reason `clickops.py`'s own
#: `_ACTION_NAME` table is spelled out: this is a small, stable protocol
#: between that module and this one, and getting a marker subtly wrong here
#: would silently misfile a refusal under the wrong channel, or under none.
_IAM_IDP_MARKER = " iam-idp: "
_TRUST_MARKER = " trust: "
_EVENTBRIDGE_MARKER = " eventbridge: "
_NO_CREDS_MARKER = ": no usable credentials"
_EVENTBRIDGE_NO_REGIONS_PREFIX = "eventbridge: no regions known"
_MARKETPLACE_PREFIX = "marketplace: "
_COST_EXPLORER_PREFIX = "cost explorer: "
_AWS_SWEEP_PREFIX = "aws sweep: "

#: `resource_types` keys that credit a discovered provider to one specific
#: per-account channel — see `collectors/clickops.py`'s own `_record` and
#: `collect_trust_relationships`/`collect_partner_event_sources`. Identity
#: providers and trust relationships both tag `source_files` with the same
#: `aws:iam:<account>` string, so the channel a row belongs to is only
#: recoverable from which of these keys `resource_types` actually holds.
_IDP_FOUND_KEYS = ("saml_provider", "oidc_provider")
_TRUST_FOUND_KEY = "assume_role_trust"
_EVENTBRIDGE_FOUND_KEY = "partner_event_source"


def _split_on_marker(line: str, marker: str) -> tuple[str, str] | None:
    """`"<account> <marker>: <message>"` -> `(account, message)`, or `None`."""
    idx = line.find(marker)
    if idx == -1:
        return None
    return line[:idx], line[idx + len(marker) :]


def _refusals_by_account(refused: Sequence[str], marker: str) -> dict[str, list[str]]:
    """`{account_id: [message, ...]}` for every refusal tagged with one channel marker."""
    out: dict[str, list[str]] = {}
    for line in refused:
        split = _split_on_marker(line, marker)
        if split is None:
            continue
        account, message = split
        out.setdefault(account, []).append(message)
    return out


def _eventbridge_refusals_by_account(refused: Sequence[str]) -> dict[str, list[str]]:
    """Like :func:`_refusals_by_account`, but the account is `account/region`."""
    out: dict[str, list[str]] = {}
    for line in refused:
        split = _split_on_marker(line, _EVENTBRIDGE_MARKER)
        if split is None:
            continue
        account_region, message = split
        account, _, region = account_region.partition("/")
        out.setdefault(account, []).append(f"{region}: {message}" if region else message)
    return out


def _unreachable_accounts(refused: Sequence[str]) -> list[tuple[str, str]]:
    """Accounts named to be swept whose session could never be established.

    Distinct from a per-channel refusal: a `"no usable credentials"` line
    means no read-only call was ever attempted for this account at all, so it
    never entered ``swept_accounts`` in the first place (Task 7's own
    accounting excludes it). It must not simply vanish from the note because
    of that exclusion — a reader who sees neither a swept-account entry nor
    this one would have no way to learn the account was even named.
    """
    out: list[tuple[str, str]] = []
    for line in refused:
        idx = line.find(_NO_CREDS_MARKER)
        if idx == -1:
            continue
        out.append((line[:idx], line[idx + 2 :]))
    return out


def _found_by_channel(providers: Sequence[Any], tag_prefix: str, keys: Sequence[str]) -> bool:
    """Whether any discovered provider carries this account/channel's tag."""
    for provider in providers:
        source_files = getattr(provider, "source_files", None) or ()
        if not any(str(tag).startswith(tag_prefix) for tag in source_files):
            continue
        resource_types = getattr(provider, "resource_types", None) or {}
        if any(key in resource_types for key in keys):
            return True
    return False


def _channel_state(label: str, refusals: list[str] | None, found: bool, unavailable: str | None = None) -> str:
    """One channel, one account, one of three outcomes an auditor can trust.

    ``refused`` and ``read, no result`` arrive from the same empty list in
    memory and are opposite claims about the world; collapsing them would
    make this note assert something nobody verified. ``unavailable`` is a
    fourth, narrower case — the channel was never even attempted, which is
    neither of the other two and must not be reported as either.
    """
    if unavailable:
        return f"  - {label}: not swept — {unavailable}"
    if refusals:
        return f"  - {label}: refused — {'; '.join(refusals)}"
    if found:
        return f"  - {label}: read"
    return f"  - {label}: read, no result"


def _accounts_swept_section(perimeter: dict[str, Any], providers: Sequence[Any]) -> list[str]:
    """Golden rule 5, applied to the four click-ops channels: never let a
    reader conclude an account or a channel was read when it was refused, or
    was read at all when nothing named it to sweep in the first place.
    """
    swept_accounts: list[str] = list(perimeter.get("swept_accounts") or [])
    clickops_refused: list[str] = list(perimeter.get("clickops_refused") or [])
    unnamed_principals: list[Any] = list(perimeter.get("unnamed_principals") or [])
    aws_sweep_configured = bool(perimeter.get("aws_sweep_configured"))

    if not (swept_accounts or clickops_refused or unnamed_principals):
        return []

    out = [
        "## Accounts swept",
        "",
        "Per-account click-ops discovery — federated identity providers, cross-account "
        "trust relationships, and EventBridge partner event sources — answers a "
        "different question for each account than a single yes/no. Three outcomes are "
        "possible for each channel in each account: **read** (the call succeeded and "
        "found something), **read, no result** (the call succeeded and found nothing), "
        "and **refused** (AWS said no to a specific action, named below). The middle one "
        "and the last one arrive as the same empty list and are opposite claims about "
        "the world — this note is the one place they are told apart.",
        "",
    ]

    idp_refusals = _refusals_by_account(clickops_refused, _IAM_IDP_MARKER)
    trust_refusals = _refusals_by_account(clickops_refused, _TRUST_MARKER)
    eventbridge_refusals = _eventbridge_refusals_by_account(clickops_refused)
    no_regions_known = any(line.startswith(_EVENTBRIDGE_NO_REGIONS_PREFIX) for line in clickops_refused)

    if swept_accounts:
        for account_id in swept_accounts:
            out.append(f"- **{account_id}**")
            out.append(
                _channel_state(
                    "Identity providers",
                    idp_refusals.get(account_id),
                    _found_by_channel(providers, f"aws:iam:{account_id}", _IDP_FOUND_KEYS),
                )
            )
            out.append(
                _channel_state(
                    "Cross-account trust",
                    trust_refusals.get(account_id),
                    _found_by_channel(providers, f"aws:iam:{account_id}", (_TRUST_FOUND_KEY,)),
                )
            )
            eb_refused = eventbridge_refusals.get(account_id)
            out.append(
                _channel_state(
                    "Partner event sources",
                    eb_refused,
                    _found_by_channel(providers, f"aws:events:{account_id}:", (_EVENTBRIDGE_FOUND_KEY,)),
                    unavailable=(
                        "no region was known to sweep (no state file or cluster named one)"
                        if no_regions_known and not eb_refused
                        else None
                    ),
                )
            )
        out.append("")
    elif aws_sweep_configured:
        aws_sweep_refusal = next((line for line in clickops_refused if line.startswith(_AWS_SWEEP_PREFIX)), None)
        if aws_sweep_refusal:
            # `assume_role_name` names a role to try in every organisation
            # account, but resolves to no accounts at all when Organizations
            # itself could not be read — nothing was ever named to sweep, which
            # is a different, earlier failure than a named account being
            # refused, and must not be described as one.
            out += [f"No account could be resolved to sweep: {aws_sweep_refusal}.", ""]
        else:
            out += [
                "Every account named in the sweep configuration was refused before a usable "
                "session existed; none of the three channels above ran anywhere.",
                "",
            ]
    else:
        out += [
            "No `--sources` file with an `aws:` block was given, so identity providers, "
            "cross-account trust relationships and partner event sources were never "
            "attempted in any account. Only AWS Marketplace billing ran for this scan, "
            "because it needs no account list — one of the four click-ops channels, not "
            "all of them.",
            "",
        ]

    unreachable = _unreachable_accounts(clickops_refused)
    if unreachable:
        out += [
            "Accounts named to be swept but never reached at all — no session could be "
            "established, so no channel above was even attempted for them:",
            "",
        ]
        out += [f"- **{account}**: {message}" for account, message in unreachable]
        out.append("")

    if unnamed_principals:
        out += [
            "## External principals we could not name",
            "",
            "An outside AWS account with standing `sts:AssumeRole` access is a fact about "
            "the perimeter whether or not a table can put a vendor's name to it. Inventing "
            "one from an account number nobody vouched for would turn a hypothesis into a "
            "fact this register has no right to assert, so it is declared as an unknown "
            "instead:",
            "",
        ]
        for entry in unnamed_principals:
            account_id, role_name, has_external_id = entry
            condition = "with" if has_external_id else "without"
            out.append(
                f"- Account `{account_id}` could not name a vendor: it has standing access via "
                f"role `{role_name}` ({condition} an `sts:ExternalId` condition)."
            )
        out.append("")

    return out


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

    aws_read = bool(perimeter.get("aws"))
    clickops_refused: list[str] = list(perimeter.get("clickops_refused") or [])
    swept_accounts: list[str] = list(perimeter.get("swept_accounts") or [])
    aws_sweep_configured = bool(perimeter.get("aws_sweep_configured"))
    ce_refusal = next((line for line in clickops_refused if line.startswith(_COST_EXPLORER_PREFIX)), None)
    marketplace_refusal = next((line for line in clickops_refused if line.startswith(_MARKETPLACE_PREFIX)), None)

    if not aws_read:
        clickops_line = "not read"
    elif not aws_sweep_configured:
        clickops_line = (
            "not swept — no `--sources` file with an `aws:` block was given, so no account list existed to sweep"
        )
    elif swept_accounts:
        clickops_line = f"swept {len(swept_accounts)} account(s) — see *Accounts swept* below"
    else:
        clickops_line = "configured, but no account was reached — see *Accounts swept* below"

    if not aws_read:
        marketplace_line = "not read"
    elif marketplace_refusal:
        marketplace_line = f"refused — {marketplace_refusal}"
    else:
        marketplace_line = "read"

    out += [
        "",
        f"- **AWS Organizations and Cost Explorer:** {'read' if aws_read else 'not read'}"
        + (f" (Cost Explorer refused: {ce_refusal})" if ce_refusal else ""),
        f"- **AWS Marketplace (billing):** {marketplace_line}",
        f"- **AWS click-ops discovery** (identity providers, cross-account trust, partner event sources): "
        f"{clickops_line}",
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
    ]
    out += _accounts_swept_section(perimeter, providers)
    out += [
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
