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

__all__ = ["refusal_kind", "to_markdown"]

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
#: Unlike the first cut of this module, these are only ever matched against a
#: refusal slice already known to belong to one account — see
#: `_Sources.swept_accounts` in cli.py — never against the flat, whole-run
#: `clickops_refused` list. A review found that the flat approach broke for an
#: account keyed by a `role_arn` (which contains a `/`) once EventBridge
#: packed `<account>/<region>` ahead of its own marker: the account could no
#: longer be recovered from the string, so the refusal was misfiled under a
#: garbled key and the real account silently defaulted to "read, no result".
_IAM_IDP_MARKER = " iam-idp: "
_TRUST_MARKER = " trust: "
_EVENTBRIDGE_MARKER = " eventbridge: "
_EVENTBRIDGE_NO_REGIONS_PREFIX = "eventbridge: no regions known"
_MARKETPLACE_PREFIX = "marketplace: "
_COST_EXPLORER_PREFIX = "cost explorer: "
_AWS_SWEEP_PREFIX = "aws sweep: "
_NO_CREDS_TEXT = ": no usable credentials"
_PARSE_FAILURE_TEXT = "unreadable trust policy"

#: The two independent `list_*` calls `collect_identity_providers` makes —
#: see `clickops.py`'s own `_ACTION_NAME` table. A denial on one of them
#: only fails *that* provider type; the other is attempted regardless (see
#: `collect_identity_providers`'s own code, one `_listed()` call per type,
#: neither gated on the other). Review finding R5: treating a single one of
#: these two denials as a whole-channel refusal renders "refused" next to a
#: register row the *other*, unaffected call itself produced.
_IDP_LIST_ACTIONS = ("iam:ListSAMLProviders", "iam:ListOpenIDConnectProviders")


def refusal_kind(line: str) -> str:
    """Classify one `clickops_refused` line the same way for every surface
    that renders one, so the console (`cli._print_perimeter`) and this
    module can never describe the same line two different ways — the
    contradiction a review found (R2): the console labelled a trust-policy
    parse failure and an unreached account "Refused" while this module
    explains, correctly, that neither is AWS refusing anything.

    Returns one of:

    - ``"not_reached"`` — no session could be established for the account
      at all (`"<account>: no usable credentials"`).
    - ``"parse_failure"`` — dora-roi's own failure to parse something AWS
      returned (`"unreadable trust policy on role ..."`); AWS said nothing.
    - ``"not_attempted"`` — a channel or sweep that was never even tried,
      for a reason the message itself names (no known region, no resolved
      account list).
    - ``"unavailable"`` — the Cost Explorer or Marketplace billing call
      itself failed, for a reason this line does not narrow further:
      `_collect_aws`'s own comment on its Cost Explorer catch (cli.py) says
      the wrapped `AwsError` covers a missing credential, a denied
      permission, *and* an unreachable region as one exception, and
      `collect_marketplace`'s last `except Exception` arm (clickops.py)
      catches whatever else a client or a connection can do. Calling either
      shape a denial would claim more than the exception that produced it
      distinguishes.
    - ``"denied"`` — everything else: a per-account identity-providers,
      trust or EventBridge call this account's own session made, where AWS
      returned an error for that one call.

    Works on a raw, whole-run `clickops_refused` line (still carrying its
    `"<account> <channel>: "` prefix) and on the same line already stripped
    of that prefix (as `_classify_refusals` produces) alike: every check
    here is a substring match on text that never appears inside the other
    two, never a match anchored to where the account happened to end.
    """
    if _NO_CREDS_TEXT in line:
        return "not_reached"
    if _PARSE_FAILURE_TEXT in line:
        return "parse_failure"
    if line.startswith(_EVENTBRIDGE_NO_REGIONS_PREFIX) or line.startswith(_AWS_SWEEP_PREFIX):
        return "not_attempted"
    if line.startswith(_COST_EXPLORER_PREFIX) or line.startswith(_MARKETPLACE_PREFIX):
        return "unavailable"
    return "denied"


#: `resource_types` keys that credit one account's own evidence to one
#: specific channel — see `collectors/clickops.py`'s `_record`,
#: `collect_trust_relationships` and `collect_partner_event_sources`. Checked
#: only against `swept_evidence[account_id]` (cli.py's `_perimeter()`), which
#: is that one account's own unmerged `collect_clickops` result — never
#: against the shared, merged provider list, which unions `resource_types` by
#: provider name across every account `merge_providers` is ever called with.
#: A review (finding C3) found that merge made two accounts sharing one
#: vendor (say, Okta federating into account A while a role in account B
#: trusts Okta's own AWS account) render as if each account had used both
#: channels, which neither did.
_IDP_FOUND_KEYS = ("saml_provider", "oidc_provider")
_TRUST_FOUND_KEY = "assume_role_trust"
_EVENTBRIDGE_FOUND_KEY = "partner_event_source"


def _classify_refusals(account_id: str, refusals: Sequence[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """One account's own refusal slice, split into (idp, trust, eventbridge, residual).

    ``refusals`` is already scoped to exactly one account by the caller
    (``_Sources.swept_accounts`` in cli.py), so no account identity is ever
    recovered from the text here — only the channel. The one exception is
    EventBridge, whose message packs ``<account>/<region>`` ahead of its own
    marker: the region is recovered by stripping the *known* ``account_id``
    as a literal prefix, never by splitting on the first ``/``, which is
    exactly what broke for an account keyed by a ``role_arn`` (review finding
    C1 — a role ARN contains a ``/`` of its own).

    A line matching none of the three markers lands in ``residual`` instead
    of being dropped — see review finding I1: a fifth channel, or a reworded
    message, must still surface somewhere, not silently leave its channel's
    default state (``read, no result``) uncontested.
    """
    idp, trust, eventbridge, residual = [], [], [], []
    for line in refusals:
        if _IAM_IDP_MARKER in line:
            idp.append(line.split(_IAM_IDP_MARKER, 1)[1])
        elif _TRUST_MARKER in line:
            trust.append(line.split(_TRUST_MARKER, 1)[1])
        elif _EVENTBRIDGE_MARKER in line:
            head, _, message = line.partition(_EVENTBRIDGE_MARKER)
            prefix = f"{account_id}/"
            region = head[len(prefix) :] if head.startswith(prefix) else ""
            eventbridge.append(f"{region}: {message}" if region else message)
        else:
            residual.append(line)
    return idp, trust, eventbridge, residual


def _found(evidence: Sequence[str], keys: Sequence[str]) -> bool:
    """Whether this account's own evidence carries any of a channel's keys."""
    return any(key in evidence for key in keys)


def _idp_line(refusals: list[str], found: bool) -> str:
    """Identity providers is really two independent list calls (SAML, OIDC)
    plus a `get` per item either one returns. A `get` denial costs one
    already-listed provider's document and nothing else (review finding I2).
    A `list` denial only costs the *whole* channel if it hits **both** list
    calls — one type denied while the other succeeded, and possibly found a
    vendor, is the same partial shape, not a total refusal (review finding
    R5: I2's own defect, one call pair over). Collapsing either into a whole
    refusal would contradict a register that still carries a row this
    channel itself produced.
    """
    list_denials = [r for r in refusals if any(action in r for action in _IDP_LIST_ACTIONS)]
    denied_list_actions = {action for action in _IDP_LIST_ACTIONS if any(action in r for r in list_denials)}
    whole = list_denials if denied_list_actions == set(_IDP_LIST_ACTIONS) else []
    partial = [r for r in refusals if r not in whole]
    if whole:
        return f"  - Identity providers: refused — {'; '.join(whole)}"
    if partial:
        return (
            f"  - Identity providers: read, but could not fully enumerate this account's "
            f"identity providers — {'; '.join(partial)}"
        )
    return f"  - Identity providers: {'read' if found else 'read, no result'}"


def _trust_line(refusals: list[str], found: bool) -> str:
    """Cross-account trust: `iam:ListRoles` denied is AWS refusing the whole
    channel. A trust policy dora-roi could not parse on one role is *this
    tool's own* failure, not AWS saying no — the section defines *refused* as
    "AWS said no to a specific action", and labelling a parse failure that
    way would assert something that did not happen (review finding I2).
    """
    whole = [r for r in refusals if "iam:ListRoles" in r]
    unparseable = [r for r in refusals if r not in whole]
    if whole:
        return f"  - Cross-account trust: refused — {'; '.join(whole)}"
    if unparseable:
        return (
            f"  - Cross-account trust: read, but could not parse {len(unparseable)} role's own trust "
            f"policy — dora-roi's own parse failure, not an AWS denial — {'; '.join(unparseable)}"
        )
    return f"  - Cross-account trust: {'read' if found else 'read, no result'}"


def _eventbridge_line(refusals: list[str], found: bool, no_regions_known: bool) -> str:
    """Partner event sources: one call type, so no partial/whole split applies.

    ``no_regions_known`` is a fourth, narrower state — the channel was never
    even attempted because no state file or cluster ever named a region —
    which is neither a clean read nor a refusal and must not be reported as
    either.
    """
    if refusals:
        return f"  - Partner event sources: refused — {'; '.join(refusals)}"
    if no_regions_known:
        return (
            "  - Partner event sources: not swept — no region was known to sweep (no state file or cluster named one)"
        )
    return f"  - Partner event sources: {'read' if found else 'read, no result'}"


def _withhold_if_uncertain(line: str, has_residual: bool) -> str:
    """A refusal this note could not attribute to a channel might in fact
    belong to the very channel a line otherwise reports cleanly — a bare
    `read` or `read, no result` claim must not stand, unqualified, next to an
    unattributed refusal for the same account. A review found exactly this:
    the residual heading disclosed the refusal without ever withdrawing the
    positive claim standing above it, which is not two facts side by side —
    it is a contradiction (R1). A line that already carries its own
    refused/partial/not-swept state is left alone: it already says something
    happened, and is not the line the unattributed refusal could be hiding
    behind.
    """
    if not has_residual:
        return line
    if any(marker in line for marker in ("refused", "could not fully enumerate", "could not parse", "not swept")):
        return line
    label = line.split(":", 1)[0].removeprefix("  - ")
    return (
        f"  - {label}: unconfirmed — this account also has a refusal this note could not "
        f"attribute to a channel (see below); it may belong here"
    )


def _accounts_swept_section(perimeter: dict[str, Any]) -> list[str]:
    """Golden rule 5, applied to the four click-ops channels: never let a
    reader conclude an account or a channel was read when it was refused, or
    was read at all when nothing named it to sweep in the first place.
    """
    swept_accounts: list[tuple[str, list[str]]] = [
        (account_id, list(refusals)) for account_id, refusals in (perimeter.get("swept_accounts") or [])
    ]
    # A list, positionally aligned with `swept_accounts` — never a dict keyed
    # by account_id. `account_id` is not guaranteed unique: two profile-only
    # sweep entries with neither `id` nor `role_arn` both collapse to the
    # literal string "unknown account" (cli.py's own fallback), and a review
    # found that a dict then silently drops the first account's evidence
    # under the second's key (R4).
    swept_evidence: list[list[str]] = [list(keys) for keys in (perimeter.get("swept_evidence") or [])]
    unreachable_accounts: list[tuple[str, str]] = [
        (account_id, message) for account_id, message in (perimeter.get("unreachable_accounts") or [])
    ]
    unnamed_principals: list[Any] = list(perimeter.get("unnamed_principals") or [])
    clickops_refused: list[str] = list(perimeter.get("clickops_refused") or [])
    aws_sweep_configured = bool(perimeter.get("aws_sweep_configured"))

    if not (swept_accounts or unreachable_accounts or unnamed_principals or aws_sweep_configured):
        return []

    out = [
        "## Accounts swept",
        "",
        "Per-account click-ops discovery — federated identity providers, cross-account "
        "trust relationships, and EventBridge partner event sources — answers a "
        "different question for each account than a single yes/no. The distinction this "
        "note exists to keep: **read, no result** (the call succeeded and found nothing) "
        "and **refused** (AWS said no to a specific action, named below) arrive as the "
        "same empty list in memory and are opposite claims about the world. A clean "
        "**read** (the call succeeded and found something) is a third, positive claim. "
        "Other lines below say more precisely what happened rather than collapsing into "
        "one of those three: a single provider's document dora-roi could not retrieve "
        "costs that one document, not the whole channel; a single role's trust policy "
        "dora-roi could not parse is this tool's own failure, not an AWS denial, and is "
        "named as such; a channel with no known region says so instead of reading as a "
        "clean read or a refusal; and a refusal this note could not attribute to any of "
        "the three channels marks each of this account's otherwise-clean lines "
        "**unconfirmed** instead of read or empty, because that unattributed refusal "
        "might belong to exactly that channel.",
        "",
    ]

    no_regions_known = any(line.startswith(_EVENTBRIDGE_NO_REGIONS_PREFIX) for line in clickops_refused)
    residual_by_account: list[tuple[str, str]] = []

    if swept_accounts:
        for index, (account_id, refusals) in enumerate(swept_accounts):
            idp_refused, trust_refused, eb_refused, residual = _classify_refusals(account_id, refusals)
            evidence = swept_evidence[index] if index < len(swept_evidence) else []
            has_residual = bool(residual)

            out.append(f"- **{account_id}**")
            out.append(_withhold_if_uncertain(_idp_line(idp_refused, _found(evidence, _IDP_FOUND_KEYS)), has_residual))
            out.append(
                _withhold_if_uncertain(_trust_line(trust_refused, _found(evidence, (_TRUST_FOUND_KEY,))), has_residual)
            )
            out.append(
                _withhold_if_uncertain(
                    _eventbridge_line(eb_refused, _found(evidence, (_EVENTBRIDGE_FOUND_KEY,)), no_regions_known),
                    has_residual,
                )
            )
            residual_by_account += [(account_id, line) for line in residual]
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
        elif unreachable_accounts:
            out += [
                "Every account named in the sweep configuration was refused before a usable "
                "session existed; none of the three channels above ran anywhere.",
                "",
            ]
        else:
            # Configured, but the `aws:` block itself named zero accounts —
            # neither `accounts:` nor `assume_role_name:` resolved to
            # anything — which is milder than either case above: nothing was
            # even attempted, let alone refused.
            out += [
                "The `aws:` block in `--sources` did not resolve to a single account to sweep — "
                "identity providers, cross-account trust relationships and partner event sources "
                "were never attempted anywhere.",
                "",
            ]
    # No final `else` for "no --sources file at all": `swept_accounts`,
    # `unreachable_accounts` and `unnamed_principals` are only ever populated
    # inside `_collect_aws`'s `if sweep is not None:` block (cli.py), which is
    # exactly `aws_sweep_configured`. So whenever `aws_sweep_configured` is
    # false, all three are guaranteed empty too, and the guard above already
    # returned before reaching this line — this case is already stated, once,
    # by the "AWS click-ops discovery" line in "What was read".

    if unreachable_accounts:
        out += [
            "Accounts named to be swept but never reached at all — no session could be "
            "established, so no channel above was even attempted for them:",
            "",
        ]
        out += [f"- **{account}**: {message}" for account, message in unreachable_accounts]
        out.append("")

    if residual_by_account:
        out += [
            "### Refusals we could not attribute to a channel",
            "",
            "Every other refusal above was recognised as belonging to one of the three "
            "channels. These were not — a reworded message, or a fifth channel this note "
            "does not yet know about — and are shown as-is rather than left to default their "
            "channel to a false *read, no result*:",
            "",
        ]
        out += [f"- **{account}**: {message}" for account, message in residual_by_account]
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
    out += _accounts_swept_section(perimeter)
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
