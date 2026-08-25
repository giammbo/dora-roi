"""Command line entry point for dora-roi.

Two commands. ``providers`` answers "what did you find?" in one screen, and is
the cheapest way to check whether the mapping knows your stack before running
anything longer. ``scan`` runs the whole pipeline and writes the four files.

Everything the CLI prints and writes obeys golden rules 3 and 5: it never
claims compliance, and it always states what it looked at. A register that
implies completeness it does not have is worse than no register, because the
shadow IT it silently omits is exactly what an examiner will ask about.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from dora_roi import __version__
from dora_roi.collectors.aws import (
    AwsError,
    OrganizationInventory,
    calling_account,
    collect_annual_expense,
    collect_organization,
    expense_for_provider,
)
from dora_roi.collectors.clickops import (
    ClickopsError,
    ExternalPrincipal,
    VendorFact,
    collect_clickops,
    collect_marketplace,
)
from dora_roi.collectors.k8s import K8sError, collect_kubernetes
from dora_roi.collectors.sources import (
    AwsAccount,
    AwsSweep,
    SourceError,
    StateSource,
    fetch_sources,
    load_aws_sweep,
    load_sources,
)
from dora_roi.collectors.tfstate import (
    DiscoveredProvider,
    TfstateError,
    merge_providers,
    parse_many,
    resolve_state_paths,
)
from dora_roi.enrichment.gleif import GleifClient, GleifError, MatchType
from dora_roi.enrichment.mapping import MappingError, ProviderMapping, load_mapping, provider_to_tpp
from dora_roi.export.preflight import PreflightError, Severity, load_prefill, preflight, summarise_findings
from dora_roi.export.xbrl_csv import ExportError, PackageName, write_package
from dora_roi.models.enums import FieldStatus, ICTServiceType, IdentifierType
from dora_roi.models.templates import (
    TEMPLATE_COLLECTIONS,
    ContractualArrangementGeneral,
    GroupEntity,
    Provenance,
    RegisterOfInformation,
    RoIRow,
    SupplyChainLink,
    ThirdPartyProvider,
)
from dora_roi.overlay.vendors import OverlayError, apply_overlay, load_overlay, overlay_template
from dora_roi.report.gap import build_gap_report, summarize, to_json, to_markdown
from dora_roi.report.methodology import refusal_kind
from dora_roi.report.methodology import to_markdown as methodology_markdown

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_UNEXPECTED = 2

DISCLAIMER = (
    "dora-roi is read-only, does not make you DORA-compliant, files nothing on your behalf, "
    "and is not legal advice. Every INFERRED value is a hypothesis that needs review before "
    "it reaches a filing."
)
PERIMETER_WARNING = (
    "The channels and accounts named above are the whole of this run's scope; nothing outside them was "
    "attempted. Being in scope is not the same as having been read cleanly — a refusal inside that scope "
    "is reported above, by name, never folded silently into a clean read. A vendor that leaves no trace "
    "anywhere in scope — a SaaS bought on a card, a contract with no infrastructure footprint at all — "
    "is exactly as invisible to this tool as it always was: discovering more here changes how much of "
    "the visible estate this tool can read, not the size of what it cannot. This register does not "
    "claim to be complete."
)

console = Console()
err = Console(stderr=True)

app = typer.Typer(
    name="dora-roi",
    help="Prefill the DORA Register of Information from your infrastructure-as-code, and report what is still missing.",
    no_args_is_help=True,
    add_completion=False,
)

_STATE_OPTION = typer.Option(None, "-s", "--state", help="Terraform/OpenTofu state file (repeatable).")
_MAPPING_OPTION = typer.Option(None, "-m", "--mapping", help="YAML overriding the packaged provider mapping.")
_SOURCES_OPTION = typer.Option(
    None, "--sources", help="YAML listing state files, including s3:// URIs with per-source credentials."
)
_OVERLAY_OPTION = typer.Option(None, "--overlay", help="vendors.yaml with the contractual data no scanner can produce.")

overlay_app = typer.Typer(help="Work with vendors.yaml, the manual overlay.", no_args_is_help=True)
app.add_typer(overlay_app, name="overlay")


@overlay_app.command("init")
def overlay_init(
    output: Path = typer.Option(Path("."), "-o", "--output", help="Where the last scan wrote inventory.json."),
    to: Path = typer.Option(Path("vendors.yaml"), "--to", help="File to write."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write a commented vendors.yaml, seeded with the providers the last scan found."""
    if to.exists() and not force:
        err.print(f"[red]Error:[/red] {to} already exists. Pass --force to overwrite it.")
        raise typer.Exit(EXIT_USER_ERROR)

    names: list[str] = []
    resolved: dict[str, dict[str, str]] = {}
    inventory = output / "inventory.json"
    if inventory.is_file():
        try:
            names = [p["name"] for p in json.loads(inventory.read_text())["providers"]]
            resolved = _already_settled(output / "roi_prefill.json", names)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            err.print(f"[yellow]Warning:[/yellow] could not read {inventory} ({e}); writing an empty template.")
    else:
        err.print(f"[yellow]Warning:[/yellow] no {inventory} found. Run `dora-roi scan` first for a seeded template.")

    to.write_text(overlay_template(names, resolved), encoding="utf-8")
    console.print(f"Wrote [bold]{to}[/bold] with {len(names)} provider block(s), all commented out.")
    console.print("[dim]Uncomment only what you can assert: every value there is recorded as FILLED.[/dim]")


@app.callback()
def callback() -> None:
    """Prefill the DORA Register of Information from your infrastructure-as-code.

    dora-roi never makes you compliant on its own: every value it produces is
    labelled FILLED, INFERRED or MISSING, and the gap report tells you which of
    the missing ones block a filing.
    """


@app.command()
def version() -> None:
    """Print the dora-roi version."""
    typer.echo(__version__)


@app.command()
def providers(
    state: list[Path] | None = _STATE_OPTION,
    mapping_file: Path | None = _MAPPING_OPTION,
) -> None:
    """List the ICT providers discovered in state, and what the mapping suggests for them."""
    state = state or []
    if not state:
        _fail(ValueError("nothing to scan: pass at least one -s/--state file."))
    try:
        state = resolve_state_paths(state)
        discovered = parse_many(state)
        mapping = load_mapping(mapping_file)
    except (TfstateError, MappingError) as e:
        _fail(e)

    table = Table(title=f"{len(discovered)} provider(s) discovered", title_justify="left")
    table.add_column("Provider")
    table.add_column("Namespace")
    table.add_column("Suggested vendor")
    table.add_column("Services")
    table.add_column("Resources", justify="right")
    table.add_column("Regions")
    for provider in discovered:
        entry = mapping.get(provider.name)
        table.add_row(
            provider.name,
            provider.namespace,
            entry.vendor if entry else "— not mapped —",
            ", ".join(entry.services) if entry else "—",
            str(provider.resource_count),
            ", ".join(sorted(provider.regions)) or "—",
        )
    console.print(table)
    _print_perimeter(_Sources(states=list(state)), gleif=False)
    console.print(f"\n[dim]{DISCLAIMER}[/dim]")


def _already_settled(prefill: Path, names: list[str]) -> dict[str, dict[str, str]]:
    """Which provider fields a scan resolved to FILLED, so the template can say so.

    Only FILLED counts. An INFERRED value is a guess, and telling somebody a
    guess is "already confirmed" is how the overlay stops being the place where
    facts are asserted.

    Matched to ``names`` by ``source_key``, never by list position. Both lists
    come from the same scan, but a vendor-discovery channel can fold a new
    provider into ``inventory.json``'s ``discovered`` order without touching
    ``roi_prefill.json``'s ``rows`` order the same way — a positional zip would
    then hand one provider's confirmed legal name and LEI to a different
    provider's block, silently and plausibly. `overlay/vendors.py` documents
    dropping the identical assumption for the same reason.
    """
    if not prefill.is_file():
        return {}
    try:
        rows = json.loads(prefill.read_text())["templates"]["B_05.01"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}

    by_code = {
        info.alias: attribute for attribute, info in ThirdPartyProvider.model_fields.items() if info.alias is not None
    }
    by_source_key = {row["source_key"]: row for row in rows if isinstance(row, dict) and row.get("source_key")}
    settled: dict[str, dict[str, str]] = {}
    for name in names:
        row = by_source_key.get(name)
        if row is None:
            continue
        settled[name] = {
            by_code[code]: str(row["values"].get(code))
            for code, entry in (row.get("provenance") or {}).items()
            if code in by_code and entry.get("status") == "FILLED" and row["values"].get(code)
        }
    return settled


@app.command()
def export(
    output: Path = typer.Option(Path("."), "-o", "--output", help="Directory holding roi_prefill.json."),
    to: Path | None = typer.Option(None, "--to", help="Where to write the package. Defaults to --output."),
    country: str | None = typer.Option(None, "--country", help="ISO 3166-1 alpha-2 of the filing entity."),
    individual: bool = typer.Option(False, "--individual", help="Individual scope. Consolidated by default."),
    check_only: bool = typer.Option(False, "--check", help="Run pre-flight and stop, writing nothing."),
    force: bool = typer.Option(False, "--force", help="Export despite blocking findings."),
    software: bool = typer.Option(True, "--software/--no-software", help="Declare dora-roi as generating software."),
) -> None:
    """Write an xBRL-CSV report package, after checking it the way a regulator would.

    Pre-flight runs first and blocking findings stop the export. That is not
    caution for its own sake: a package that is well-formed and wrong is worse
    than no package, because it looks filed.
    """
    try:
        _export(output, to, country, individual, check_only, force, software)
    except (PreflightError, ExportError) as e:
        _fail(e)
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001 - the CLI boundary is where unexpected stops
        err.print(f"[red]Unexpected error:[/red] {type(e).__name__}: {e}")
        raise typer.Exit(EXIT_UNEXPECTED) from e


def _export(
    output: Path,
    to: Path | None,
    country: str | None,
    individual: bool,
    check_only: bool,
    force: bool,
    software: bool,
) -> None:
    roi = load_prefill(output / "roi_prefill.json")
    findings = preflight(roi)
    summary = summarise_findings(findings)

    for finding in [f for f in findings if f.severity is Severity.BLOCKING][:20]:
        err.print(f"[red]BLOCKING[/red] {finding.template}.{finding.field} {finding.message}")
    console.print(f"[red]{summary['blocking']} blocking[/red] · [yellow]{summary['warning']} warning[/yellow]")

    if check_only:
        console.print("\n[dim]--check: nothing was written.[/dim]")
        raise typer.Exit(EXIT_USER_ERROR if summary["blocking"] else EXIT_OK)

    if summary["blocking"] and not force:
        err.print(
            f"\n[red]Refusing to export:[/red] {summary['blocking']} blocking finding(s). "
            f"A package that is well-formed and wrong is worse than no package, because it looks filed. "
            f"Fix them, or pass --force if you know better than this tool."
        )
        raise typer.Exit(EXIT_USER_ERROR)

    entity = roi.entity
    if entity is None or not entity.lei or not entity.reporting_date:
        raise ExportError(
            "the package name needs the filing entity's LEI and reporting date (B_01.01.0010 and 0060). "
            "Set entity.lei and entity.reporting_date in the overlay and re-run scan."
        )
    resolved_country = (country or entity.country or "").upper()
    if not resolved_country:
        raise ExportError("no country for the package name: set entity.country in the overlay, or pass --country.")

    name = PackageName(
        lei=entity.lei,
        consolidated=not individual,
        country=resolved_country,
        reference_date=entity.reporting_date,
        # 17 digits, as the EBA sample writes it.
        timestamp=datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S") + f"{datetime.now(tz=UTC).microsecond // 1000:03d}",
    )
    target = write_package(
        roi,
        name,
        to or output,
        generating_software=f"dora-roi {__version__}" if software else None,
    )
    console.print(f"\nWrote [bold]{target}[/bold]")
    if force and summary["blocking"]:
        err.print("[yellow]Warning:[/yellow] exported with --force despite blocking findings. Do not file this blind.")
    console.print(f"[dim]{DISCLAIMER}[/dim]")


@app.command()
def check(
    output: Path = typer.Option(Path("."), "-o", "--output", help="Directory holding roi_prefill.json."),
    fail_on: str = typer.Option("none", "--fail-on", help="Exit non-zero on findings: `blocking`, `any`, or `none`."),
    gleif: bool = typer.Option(False, "--gleif/--no-gleif", help="Confirm each LEI against GLEIF. Off by default."),
) -> None:
    """Check a prefill the way a regulator would, before you file it.

    This began life as `export --check`, and stayed a standalone command:
    checking a prefill is useful on its own, and folding it into `export` would
    make the one command you run before you are ready the one named for the
    step you are not ready for.
    """
    if fail_on not in {"none", "blocking", "any"}:
        err.print(f"[red]Error:[/red] --fail-on must be none, blocking or any, not {fail_on!r}.")
        raise typer.Exit(EXIT_USER_ERROR)
    try:
        _check(output, fail_on, gleif)
    except PreflightError as e:
        _fail(e)
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001 - the CLI boundary is where unexpected stops
        err.print(f"[red]Unexpected error:[/red] {type(e).__name__}: {e}")
        raise typer.Exit(EXIT_UNEXPECTED) from e


def _check(output: Path, fail_on: str, use_gleif: bool) -> None:
    roi = load_prefill(output / "roi_prefill.json")

    client = None
    if use_gleif:
        try:
            client = GleifClient()
        except Exception as e:  # noqa: BLE001 - a GLEIF outage must not fail a check
            err.print(f"[yellow]Warning:[/yellow] GLEIF unavailable, checking LEIs offline only ({e}).")

    try:
        findings = preflight(roi, gleif=client)
    finally:
        if client is not None:
            client.close()

    summary = summarise_findings(findings)
    table = Table(title="Pre-flight", title_justify="left")
    table.add_column("Severity")
    table.add_column("Template")
    table.add_column("Field")
    table.add_column("Row")
    table.add_column("What")
    for finding in findings[:40]:
        colour = "red" if finding.severity is Severity.BLOCKING else "yellow"
        table.add_row(
            f"[{colour}]{finding.severity}[/{colour}]",
            finding.template or "—",
            finding.field or "—",
            finding.row_key or "—",
            finding.message,
        )
    console.print(table)
    if len(findings) > 40:
        console.print(f"[dim]… and {len(findings) - 40} more. The full list is in gap-report.json.[/dim]")

    console.print(f"\n[red]{summary['blocking']} blocking[/red] · [yellow]{summary['warning']} warning[/yellow]")
    if summary["blocking"]:
        console.print("[dim]Blocking means a filing built from this register is expected to be refused.[/dim]")
    console.print(f"\n[dim]{DISCLAIMER}[/dim]")

    if (fail_on == "blocking" and summary["blocking"]) or (fail_on == "any" and summary["total"]):
        raise typer.Exit(EXIT_USER_ERROR)


@app.command()
def scan(
    state: list[Path] | None = _STATE_OPTION,
    output: Path = typer.Option(Path("."), "-o", "--output", help="Directory for the four output files."),
    gleif: bool = typer.Option(False, "--gleif/--no-gleif", help="Resolve LEIs against GLEIF. Off by default."),
    mapping_file: Path | None = _MAPPING_OPTION,
    overlay_file: Path | None = _OVERLAY_OPTION,
    aws: bool = typer.Option(False, "--aws", help="Also read AWS Organizations and Cost Explorer (read-only)."),
    aws_profile: str | None = typer.Option(None, "--aws-profile", help="Named AWS profile to use."),
    k8s: bool = typer.Option(False, "--k8s", help="Also read a Kubernetes cluster (read-only)."),
    k8s_context: str | None = typer.Option(None, "--k8s-context", help="kubeconfig context. Implies --k8s."),
    kubeconfig: Path | None = typer.Option(None, "--kubeconfig", help="kubeconfig file. Implies --k8s."),
    sources_file: Path | None = _SOURCES_OPTION,
) -> None:
    """Discover, enrich and prefill, then write the register and the gap report."""
    try:
        remote = load_sources(sources_file) if sources_file else []
        aws_sweep = load_aws_sweep(sources_file) if sources_file else None
    except SourceError as e:
        _fail(e)
    sources = _Sources(
        states=list(state or []),
        remote=remote,
        aws=aws,
        aws_profile=aws_profile,
        aws_sweep=aws_sweep,
        k8s=k8s or k8s_context is not None or kubeconfig is not None,
        k8s_context=k8s_context,
        kubeconfig=kubeconfig,
    )
    if not sources.any():
        _fail(ValueError("nothing to scan: pass -s/--state, --sources, --aws or --k8s."))
    try:
        _scan(sources, output, gleif, mapping_file, overlay_file)
    except (TfstateError, MappingError, OverlayError, AwsError, ClickopsError, K8sError, SourceError) as e:
        _fail(e)
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001 - the CLI boundary is where unexpected stops
        err.print(f"[red]Unexpected error:[/red] {type(e).__name__}: {e}")
        err.print("[dim]This is a bug. Please open an issue with the command you ran.[/dim]")
        raise typer.Exit(EXIT_UNEXPECTED) from e


@dataclass
class _Sources:
    """What this run was told to look at. Also the perimeter it must declare."""

    states: list[Path]
    remote: list[StateSource] = field(default_factory=list)
    aws: bool = False
    aws_profile: str | None = None
    #: Which accounts the vendor-discovery channels sweep, and whose
    #: credentials — the ``aws:`` block of a ``--sources`` file. ``None`` when
    #: no such file was given: marketplace still runs (it needs only the
    #: top-level profile), but the per-account channels have no account list
    #: to sweep without one.
    aws_sweep: AwsSweep | None = None
    k8s: bool = False
    k8s_context: str | None = None
    kubeconfig: Path | None = None

    #: uri -> the local file it was fetched to. Empty until the scan resolves them.
    fetched: dict[str, Path] = field(default_factory=dict)

    #: What was in reach and deliberately not read, for the methodology note.
    excluded: list[str] = field(default_factory=list)

    #: What the vendor-discovery channels said no to: a bad profile, an
    #: unassumable role, a denied IAM or Cost Explorer call. Task 8 renders
    #: this in the methodology note the same way `excluded` already is. Kept
    #: around even now that `swept_accounts`/`unreachable_accounts` carry the
    #: per-account facts, because the *global* refusals — Cost Explorer,
    #: Marketplace, `assume_role_name` never resolving to an account, no
    #: region ever known for EventBridge — belong to no single account.
    clickops_refused: list[str] = field(default_factory=list)
    #: External AWS accounts with standing access (a trust policy naming them)
    #: that no table could name as a vendor. Task 8 raises this as a finding.
    unnamed_principals: list[ExternalPrincipal] = field(default_factory=list)
    #: (account_id, refusals recorded during that one account's own sweep),
    #: for every account a session was actually usable for. The refusal list
    #: is `sources.clickops_refused[before:after]` for that account's own
    #: `collect_clickops` call — captured by identity, never reconstructed
    #: from the flat `clickops_refused` list by parsing an account id back out
    #: of a message string. A review of the first cut of this field found
    #: exactly why that reconstruction cannot be trusted: an account keyed by
    #: a `role_arn` contains a `/`, and the EventBridge refusal format packs
    #: `<account>/<region>` into one field, so splitting on the first `/`
    #: silently misfiled the refusal under the wrong account and rendered a
    #: denied call as a clean empty result.
    swept_accounts: list[tuple[str, list[str]]] = field(default_factory=list)
    #: The raw, unmerged providers `collect_clickops` returned for one
    #: account, before `_fold_in` folds them into the shared, cross-account
    #: register — one entry per account, positionally aligned with
    #: `swept_accounts` (index *i* here is that account's own evidence).
    #: A list, not a dict keyed by account_id: `account_id` is not
    #: guaranteed unique (two profile-only sweep entries with neither `id`
    #: nor `role_arn` both fall back to the literal string "unknown
    #: account"), and a review found that a dict silently drops the first
    #: such account's evidence under the second's key (R4) — ugly before,
    #: lossy once this field existed to be keyed at all.
    #:
    #: `tfstate.merge_providers` unions `resource_types` and `source_files`
    #: by provider name across *every* account and channel it is ever
    #: called with (by design — it is what lets one vendor's evidence from
    #: two accounts become one row) which also destroys, once merged, which
    #: channel in which account actually found something. The methodology
    #: note needs that association kept apart, per account, or two accounts
    #: sharing one vendor render as if each had every channel the other
    #: actually used.
    swept_evidence: list[list[DiscoveredProvider]] = field(default_factory=list)
    #: (account_id, message) for every account named to be swept whose
    #: session could never be established at all — a `boto3.Session` that
    #: failed to construct, or a `role_arn` that could not be assumed. Kept
    #: apart from `swept_accounts`: that account was never reached, which is
    #: a different, earlier fact than being reached and refused on a channel.
    unreachable_accounts: list[tuple[str, str]] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.states) or bool(self.remote) or self.aws or self.k8s

    @property
    def state_files(self) -> list[Path]:
        """Everything to parse: local paths plus whatever was fetched."""
        return [*self.states, *self.fetched.values()]

    @property
    def origins(self) -> dict[Path, str]:
        """Local path -> what to call it in the report."""
        return {path: uri for uri, path in self.fetched.items()}

    @property
    def declared(self) -> list[str]:
        """What to name in the perimeter: the URI, never the scratch path it landed in."""
        return sorted([str(p) for p in self.states] + list(self.fetched))

    def describe(self) -> list[str]:
        lines = [f"Terraform state: {where}" for where in self.declared]
        if len(self.declared) > 6:
            lines = [f"Terraform state: {len(self.declared)} files"] + [f"  {w}" for w in self.declared]
        if self.aws:
            lines.append(f"AWS Organizations + Cost Explorer (profile: {self.aws_profile or 'default'})")
            lines.append("AWS Marketplace (billing)")
            # The other three click-ops channels — identity providers, trust
            # relationships, partner event sources — need an `aws:` sweep
            # block to know which accounts to visit at all; a bare `--aws`
            # with no `--sources` file runs Marketplace alone, and this line
            # is what stops that reading as "every AWS channel was swept".
            if self.aws_sweep is not None:
                lines.append(self._click_ops_discovery_line())
            else:
                lines.append("AWS click-ops discovery: not swept (no --sources aws: block given)")
        if self.k8s:
            lines.append(f"Kubernetes (context: {self.k8s_context or 'current'})")
        return lines

    def _click_ops_discovery_line(self) -> str:
        """C2: "swept N account(s)" alone reads as "N accounts read cleanly" —
        it said nothing about `swept_accounts` entries that were refused on
        every channel, or accounts named to sweep that were never reached at
        all. Both belong on the terminal, not only in methodology.md, because
        this line is the one every user sees on every run.

        Counts only refusals `refusal_kind` calls `"denied"` — a review
        found the previous count included any non-empty refusal list, so a
        trust-policy parse failure alone was enough to print "1 with at
        least one channel refused" here while `_print_refusals` (below, same
        run) explains, correctly, that the same line is not a refusal at all
        (N2).
        """
        refused_count = sum(
            1 for _, refusals in self.swept_accounts if any(refusal_kind(r) == "denied" for r in refusals)
        )
        base = f"AWS click-ops discovery: swept {len(self.swept_accounts)} account(s)"
        caveats = []
        if refused_count:
            caveats.append(f"{refused_count} with at least one channel refused")
        if self.unreachable_accounts:
            caveats.append(f"{len(self.unreachable_accounts)} named but never reached")
        if not caveats:
            return base
        return f"{base} ({'; '.join(caveats)} — see methodology.md)"


def _scan(
    sources: _Sources,
    output: Path,
    use_gleif: bool,
    mapping_file: Path | None,
    overlay_file: Path | None = None,
) -> None:
    if sources.states:
        sources = replace(sources, states=resolve_state_paths(sources.states))
    scratch: Path | None = None
    try:
        if sources.remote:
            # A scratch directory, not the output one: these are copies of
            # somebody else's state and have no business surviving the run.
            scratch = Path(tempfile.mkdtemp(prefix="dora-roi-state-"))
            excluded: list[str] = []
            fetched = fetch_sources(sources.remote, scratch, excluded=excluded)
            sources = replace(sources, excluded=excluded)
            sources = replace(sources, fetched={uri: path for uri, path in fetched})
            console.print(f"Fetched [bold]{len(fetched)}[/bold] state file(s) from {len(sources.remote)} source(s).")

        groups = [parse_many(sources.state_files, sources.origins)] if sources.state_files else []
    finally:
        # Terraform state routinely carries plaintext credentials. Nothing past
        # the parse needs the files — the perimeter names the URI, never the
        # scratch path — so a read-only tool has no business leaving a copy of
        # somebody's secrets in /tmp after it has finished reading them.
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    if sources.k8s:
        groups.append(
            collect_kubernetes(
                context=sources.k8s_context, kubeconfig=str(sources.kubeconfig) if sources.kubeconfig else None
            )
        )
    discovered = merge_providers(groups)
    mapping = load_mapping(mapping_file)

    rows = [provider_to_tpp(provider, mapping) for provider in discovered]

    # Before GLEIF, not after: a vendor Marketplace or the account sweep finds
    # that Terraform never mentioned still deserves an LEI lookup, and
    # `_collect_aws` is what appends its row. Running GLEIF first would leave
    # every clickops-discovered provider without one, silently, just because
    # it was found a few lines later than everything else.
    group_entities: list[GroupEntity] = []
    if sources.aws:
        group_entities = _collect_aws(discovered, rows, mapping, sources)

    if use_gleif:
        _enrich_with_gleif(rows)

    arrangements, links = _synthesise_arrangements(discovered, mapping)
    roi = RegisterOfInformation(
        arrangements=arrangements, providers=rows, supply_chain=links, group_entities=group_entities
    )

    # Last, so a human assertion overwrites every machine guess before anything
    # is measured or written.
    if overlay_file is not None:
        overlay = load_overlay(overlay_file)
        for warning in apply_overlay(roi, overlay):
            err.print(f"[yellow]Warning:[/yellow] {warning}")

    # After the overlay, never before: the provider code it carries is usually
    # the only one there is. B_05.02 keys on 0010/0020/0030/0050/0060, so a link
    # that knows no provider code is a row of pure keys, which xBRL-CSV cannot
    # represent at all.
    _join_supply_chain(roi)

    entries = build_gap_report(roi)
    summary = summarize(entries)

    output.mkdir(parents=True, exist_ok=True)
    perimeter = _perimeter(sources, use_gleif, overlay_file)
    _write(output / "inventory.json", json.dumps(_inventory(discovered, perimeter), indent=2))
    _write(output / "roi_prefill.json", json.dumps(_prefill(roi, perimeter), indent=2))
    _write(output / "gap-report.md", to_markdown(entries))
    _write(output / "gap-report.json", to_json(entries))
    _write(
        output / "methodology.md",
        methodology_markdown(entries, perimeter, discovered, sources.excluded, version=__version__),
    )

    _print_summary(discovered, summary, output)
    _print_perimeter(sources, use_gleif)
    console.print(f"\n[dim]{DISCLAIMER}[/dim]")


def _enrich_with_gleif(rows: list[ThirdPartyProvider]) -> None:
    """Resolve LEIs. Best-effort: a GLEIF outage degrades the scan, never fails it."""
    try:
        client = GleifClient()
    except Exception as e:  # noqa: BLE001 - constructing a client must not kill a scan
        err.print(f"[yellow]Warning:[/yellow] GLEIF unavailable, continuing without LEIs ({e}).")
        return

    with client:
        for row in rows:
            if not row.legal_name:
                continue
            try:
                match = client.best_match(row.legal_name)
            except GleifError as e:
                err.print(f"[yellow]Warning:[/yellow] GLEIF lookup for {row.legal_name!r} failed: {e}")
                continue
            if match is None:
                continue

            # A provenance is never stronger than its weakest input. GLEIF
            # confirms that *the name we searched* has this LEI — and the name
            # usually comes from the packaged mapping, which is a guess. So the
            # LEI of an entity we chose is not the same claim as the identity of
            # the counterparty, and only an overlay-asserted name makes it one.
            #
            # "Asserted" means the overlay specifically, not merely FILLED: a
            # FILLED legal_name can now also come from an AWS Marketplace
            # billing fact (`_apply_vendor_facts`, source "aws:ce") — a real
            # fact about who invoices you, but not a human's word for which
            # entity they contracted with, which is the one thing this branch
            # exists to require. Checking `status_of` alone would let a
            # billing line pass as an overlay assertion the moment
            # `_collect_aws` runs before this function.
            exact = match.match_type is MatchType.EXACT
            legal_name_provenance = row.provenance.get("legal_name")
            asserted = legal_name_provenance is not None and legal_name_provenance.source == "overlay"
            status = FieldStatus.FILLED if (exact and asserted) else FieldStatus.INFERRED

            if not exact:
                note = (
                    f"fuzzy GLEIF match: searched {row.legal_name!r}, got {match.legal_name!r}. Confirm before filing."
                )
            elif asserted:
                note = "GLEIF exact match on the legal name asserted in the overlay."
            else:
                # The name searched came from somewhere other than a human
                # asserting it in the overlay — the packaged mapping's guess
                # in the common case, but as of the AWS vendor-discovery
                # channels it can also be a billing fact (source "aws:ce"):
                # real, but a statement about who invoices you, not about who
                # you contracted with. Naming the actual source keeps this
                # note true in both cases instead of overclaiming "guessed"
                # for a name AWS itself supplied.
                origin = (
                    "a billing fact (AWS knows who it invoices, not who you signed a contract with)"
                    if legal_name_provenance is not None and legal_name_provenance.source == "aws:ce"
                    else "the packaged provider mapping, not from your contract"
                )
                note = (
                    f"GLEIF confirms that {match.legal_name!r} holds this LEI. The name itself came from "
                    f"{origin} — so what is established is the LEI of the entity dora-roi named, not that "
                    f"it is your counterparty. Assert the legal name in the overlay to settle it."
                )

            row.identification_code = match.lei
            row.mark("identification_code", status, source="gleif", note=note)
            row.type_of_code = "LEI"
            row.mark("type_of_code", status, source="gleif", note=note)
            if match.country:
                row.headquarters_country = match.country
                row.mark("headquarters_country", status, source="gleif", note=note)
            if exact and asserted and match.legal_name:
                # Adopting GLEIF's spelling is safe only where the name was ours
                # to confirm. Overwriting a guess with a matching guess adds
                # nothing and hides where the name came from.
                row.legal_name = match.legal_name
                row.mark("legal_name", FieldStatus.FILLED, source="gleif", note=note)

            if match.status and match.status != "ACTIVE":
                err.print(
                    f"[yellow]Warning:[/yellow] LEI {match.lei} for {match.legal_name!r} "
                    f"is {match.status}, not ACTIVE. A filing will be rejected on it."
                )


def _synthesise_arrangements(
    discovered: list[DiscoveredProvider],
    mapping: dict[str, ProviderMapping],
) -> tuple[list[ContractualArrangementGeneral], list[SupplyChainLink]]:
    """One placeholder arrangement per provider, plus its rank-1 supply chain.

    These references are invented — a real register uses your own contract
    numbers — so they are INFERRED and the note says to replace them. They exist
    because B_02.01.0010 is the key the rest of the register joins on: without a
    reference, B_05.02 and B_02.02 have nothing to point at and the gap report
    cannot show the shape of what is missing.
    """
    arrangements: list[ContractualArrangementGeneral] = []
    links: list[SupplyChainLink] = []
    for provider in discovered:
        reference = f"ARR-{provider.name.upper()}-001"
        arrangement = ContractualArrangementGeneral(arrangement_reference=reference, source_key=provider.name)
        arrangement.mark(
            "arrangement_reference",
            FieldStatus.INFERRED,
            source="dora-roi",
            note="placeholder reference generated by the scan; replace with your contract reference",
        )
        arrangements.append(arrangement)

        entry = mapping.get(provider.name)
        services: list[ICTServiceType | None] = list(entry.services) if entry and entry.services else [None]
        for service in services:
            link = SupplyChainLink(
                arrangement_reference=reference, service_type=service, rank=1, source_key=provider.name
            )
            link.mark(
                "arrangement_reference",
                FieldStatus.INFERRED,
                source="dora-roi",
                note="joined to the arrangement in B_02.01; inherits whatever that reference is worth",
            )
            link.mark(
                "rank",
                FieldStatus.INFERRED,
                source="dora-roi",
                note="rank 1 is the direct provider; sub-contractors (rank > 1) are not discoverable from state",
            )
            if service is not None:
                link.mark(
                    "service_type",
                    FieldStatus.INFERRED,
                    source="mapping",
                    note="suggested classification — assigning S01-S19 is your regulatory responsibility",
                )
            links.append(link)

    return arrangements, links


def _collect_aws(
    discovered: list[DiscoveredProvider],
    rows: list[ThirdPartyProvider],
    mapping: dict[str, ProviderMapping],
    sources: _Sources,
) -> list[GroupEntity]:
    """Organizations, Cost Explorer, and every vendor-discovery channel: marketplace and the account sweep.

    Every one of these degrades to a warning or a ``refused`` entry of its own:
    AWS being unreachable, or the caller lacking one read permission, is not a
    reason to throw away a scan that already read the state files, and one
    channel failing is not a reason to skip the others.

    ``discovered`` and ``rows`` are mutated in place: a vendor found only by
    Marketplace, an identity provider, an EventBridge partner or a trust
    policy has no Terraform footprint to have built a row from already, and
    :func:`_fold_in` is what gives it one — the entire point of this module,
    per its own docstring, since most of what it finds appears in no
    ``required_providers`` block at all.
    """
    profile = sources.aws_profile
    entities: list[GroupEntity] = []
    organization: OrganizationInventory | None = None
    try:
        organization = collect_organization(profile=profile)
    except AwsError as e:
        err.print(f"[yellow]Warning:[/yellow] AWS Organizations unavailable: {e}")
    else:
        for account in organization.accounts:
            entity = GroupEntity(name=account.name, hierarchy=" / ".join(account.ou_path))
            entity.mark("name", FieldStatus.INFERRED, source="aws:organizations", note=account.caveat)
            entity.mark("hierarchy", FieldStatus.INFERRED, source="aws:organizations", note=account.caveat)
            entities.append(entity)

    # The account the credentials belong to — not the organisation's payer, which
    # is a different account whenever the scan runs from a member profile.
    billed = calling_account(profile)
    try:
        report = collect_annual_expense(profile=profile, payer_accounts=[billed] if billed else None)
    except (AwsError, NameError) as e:
        # `collect_annual_expense` now wraps botocore's own exceptions into
        # `AwsError` itself (a missing credential, a denied permission, an
        # unreachable region), so this catch is narrow on purpose: anything
        # else is a genuine bug in that function and must surface as one,
        # not be reported here as "Cost Explorer unavailable" and hidden from
        # the CLI's own "this is a bug" path.
        err.print(f"[yellow]Warning:[/yellow] AWS Cost Explorer unavailable: {e}")
        sources.clickops_refused.append(f"cost explorer: {e}")
        report = None

    if report is not None:
        master = organization.master_account_id if organization is not None else None
        if billed and master and billed != master:
            err.print(
                f"[yellow]Warning:[/yellow] Cost Explorer was called from account {billed}, which is not the "
                f"organisation's payer ({master}). The figure covers that account's own spend only — for the "
                f"whole organisation, re-run with the payer's profile."
            )

        for provider, row in zip(discovered, rows, strict=True):
            amount = expense_for_provider(provider.name, report)
            if amount is None:
                continue
            # AWS's own total needs no guess: the state says `hashicorp/aws` and the
            # bill says AWS. A Marketplace seller is matched by name against our own
            # table, so its figure inherits that table's uncertainty.
            direct = provider.name == "aws"
            status = FieldStatus.FILLED if direct else FieldStatus.INFERRED
            note = (
                report.provenance_note
                if direct
                else (
                    f"{report.provenance_note} Attributed to this provider by matching the seller name on "
                    f"the Marketplace line, which is dora-roi's own table and not a billing relationship "
                    f"AWS asserts."
                )
            )
            row.total_annual_expense = amount
            row.mark("total_annual_expense", status, source="aws:ce", note=note)
            if report.currency:
                row.currency = report.currency
                row.mark("currency", status, source="aws:ce", note=note)

    # Marketplace answers for the payer, once, regardless of whether an `aws:`
    # sweep block was ever configured — it needs nothing beyond the top-level
    # profile, and it is the channel that makes a bare `--aws`, no state files
    # at all, a scan worth running.
    marketplace_providers, facts = collect_marketplace(profile=profile, refused=sources.clickops_refused)
    _fold_in(discovered, rows, mapping, marketplace_providers)

    own_accounts = frozenset(a.account_id for a in organization.accounts) if organization is not None else frozenset()
    sweep = sources.aws_sweep
    if sweep is not None:
        accounts = _sweep_accounts(sweep, organization)
        if not accounts and sweep.assume_role_name and organization is None:
            sources.clickops_refused.append(
                "aws sweep: assume_role_name is set but AWS Organizations was unreachable, so there is no "
                "account list to assume it into."
            )
        regions = frozenset(region for provider in discovered for region in provider.regions)
        if not regions:
            # EventBridge is regional and this channel only ever sweeps regions
            # a state file (or Kubernetes) already named — see collect_clickops's
            # own docstring. On a no-IaC scan that set is empty, so the
            # EventBridge channel silently never runs for any account in the
            # sweep. Silence and "checked, found nothing" are opposite claims;
            # this is the former, and it must say so.
            sources.clickops_refused.append(
                "eventbridge: no regions known (no state file or cluster named one), so partner event "
                "sources were not swept in any account."
            )
        for sweep_account in accounts:
            account_id = sweep_account.id or sweep_account.role_arn or "unknown account"
            # `collect_clickops` never raises; the only way to learn a session
            # could not even be created for this account is to look for the
            # specific refusal it appends on that path. `before`/`after` slices
            # `clickops_refused` to exactly the messages this one call
            # produced — the per-account refusal set, by construction, with
            # nothing to reconstruct from a string afterwards.
            before = len(sources.clickops_refused)
            swept, unknown = collect_clickops(
                profile=sweep_account.profile or sweep.profile,
                role_arn=sweep_account.role_arn,
                account_id=account_id,
                own_accounts=own_accounts,
                regions=regions,
                refused=sources.clickops_refused,
            )
            account_refusals = sources.clickops_refused[before:]
            _fold_in(discovered, rows, mapping, swept)
            sources.unnamed_principals.extend(unknown)
            session_failure = next(
                (entry for entry in account_refusals if entry.startswith(f"{account_id}: no usable credentials")),
                None,
            )
            if session_failure is not None:
                # The session never came up at all: this account was not
                # reached, and must not read as one that was reached and
                # simply had nothing — `unreachable_accounts`'s own docstring
                # states this requirement. Recorded by identity, keyed off the
                # exact `account_id` this call used, never by re-parsing it
                # out of the message afterwards.
                sources.unreachable_accounts.append((account_id, session_failure[len(f"{account_id}: ") :]))
            else:
                # Reached, whether or not every channel inside it succeeded —
                # a bad profile with dead-but-present credentials (no
                # `role_arn`, so `boto3.Session()` never validates them until
                # the first call) can still make every channel below fail, but
                # each failure is then a per-channel refusal in
                # `account_refusals`, not a session-level one. Recording the
                # refusal list keyed by this exact `account_id` — never a
                # value reconstructed by splitting the refusal text — is what
                # lets the methodology note render each channel's own true
                # state even for an account that is technically "swept" but
                # refused on every one of them.
                sources.swept_accounts.append((account_id, account_refusals))
                # Appended in lockstep with `swept_accounts`, never keyed by
                # `account_id` — see the field's own docstring for why (R4).
                sources.swept_evidence.append(list(swept))

    _apply_vendor_facts(rows, facts)
    return entities


def _sweep_accounts(sweep: AwsSweep, organization: OrganizationInventory | None) -> list[AwsAccount]:
    """Which accounts the per-account channels read: explicit, or every member via one role.

    Explicit accounts say exactly what will be read and always win outright —
    the same "explicit beats a shortcut" rule :class:`.sources.AwsSweep`
    documents for itself. The ``assume_role_name`` shortcut only ever fires
    when nothing explicit was given *and* there is an organisation to
    enumerate: asking to assume a role in "every account" means nothing
    without a list of accounts to assume it in.
    """
    if sweep.accounts:
        return list(sweep.accounts)
    if sweep.assume_role_name and organization is not None:
        return [
            AwsAccount(
                id=account.account_id,
                role_arn=f"arn:aws:iam::{account.account_id}:role/{sweep.assume_role_name}",
            )
            for account in organization.accounts
        ]
    return []


def _fold_in(
    discovered: list[DiscoveredProvider],
    rows: list[ThirdPartyProvider],
    mapping: dict[str, ProviderMapping],
    new: list[DiscoveredProvider],
) -> None:
    """Add what a vendor-discovery channel found, without duplicating a row.

    A vendor a channel finds under a key some other source already claimed —
    Marketplace billing ``datadog`` next to a Terraform ``datadog`` provider
    block, or the same identity provider seen in two swept accounts — is the
    same vendor, not two: :func:`.tfstate.merge_providers` folds the evidence
    together, and the row that already exists for it is what
    :func:`_apply_vendor_facts` updates next. Only a name genuinely new to
    this scan earns a fresh, INFERRED row here.
    """
    if not new:
        return
    known = {provider.name for provider in discovered}
    discovered[:] = merge_providers([discovered, new])
    for provider in discovered:
        if provider.name not in known:
            rows.append(provider_to_tpp(provider, mapping))


def _apply_vendor_facts(rows: list[ThirdPartyProvider], facts: list[VendorFact]) -> None:
    """Turn authoritative billing facts into FILLED fields.

    This lives here and not in :func:`.mapping.provider_to_tpp` on purpose.
    That function builds a row from the provider mapping, which is a table of
    guesses, and it must stay structurally incapable of producing a FILLED
    value even by accident — golden rule 1 is worth more as a property of the
    code than as a review comment. Authoritative facts flow in from outside
    it instead, applied here, after every row already exists.

    The overlay still wins, per field, independently. A billing API knows who
    invoices you; only a human holding the contract knows which entity was
    actually signed with, and the two are different companies more often than
    the bill suggests — legal name always defers to an existing FILLED value.
    Annual spend is a different claim with its own guard: an overlay that
    asserts the legal name but never touched the expense (or the reverse)
    must not have the untouched field silently overwritten just because its
    sibling was already settled.
    """
    by_key = {fact.key: fact for fact in facts}
    for row in rows:
        fact = by_key.get(row.source_key or "")
        if fact is None:
            continue

        if row.status_of("legal_name") is not FieldStatus.FILLED:
            row.legal_name = fact.legal_name
            row.mark(
                "legal_name",
                FieldStatus.FILLED,
                source=fact.source,
                note="seller of record on the AWS Marketplace charges for this account",
            )

        # Grouped by LEGAL_ENTITY_NAME and filtered to the Marketplace billing
        # entity, this figure is strictly better provenance than the
        # substring-matched INFERRED estimate `_collect_aws` writes above for
        # a vendor known only from Terraform — shipping a producer
        # (`VendorFact.annual_spend`) with no consumer would be worse than not
        # collecting it at all.
        if fact.annual_spend is not None and row.status_of("total_annual_expense") is not FieldStatus.FILLED:
            note = (
                f"AWS Marketplace charges billed under {fact.legal_name!r}, grouped by LEGAL_ENTITY_NAME "
                f"and filtered to the Marketplace billing entity."
            )
            row.total_annual_expense = fact.annual_spend
            row.mark("total_annual_expense", FieldStatus.FILLED, source=fact.source, note=note)
            if fact.currency:
                row.currency = fact.currency
                row.mark("currency", FieldStatus.FILLED, source=fact.source, note=note)


def _join_supply_chain(roi: RegisterOfInformation) -> None:
    """Complete each supply-chain link: who provides, and who receives.

    At rank 1 the recipient is the filing entity itself — that is what rank 1
    means. Leaving 0060 empty is not merely incomplete: it is a key column, so
    the row's context never closes and the EBA taxonomy rejects the fact hanging
    off it. The XBRL processor is what surfaced that; nothing in the written
    specification says it in those terms.
    """
    providers = {row.source_key: row for row in roi.providers if row.source_key}
    entity_lei = roi.entity.lei if roi.entity is not None else None
    for link in roi.supply_chain:
        if entity_lei and link.rank == 1 and not link.recipient_code:
            link.recipient_code = entity_lei
            link.type_of_recipient_code = IdentifierType.LEI.value
            note = "rank 1: the recipient is the filing entity (B_01.01.0010)"
            link.mark("recipient_code", FieldStatus.INFERRED, source="dora-roi", note=note)
            link.mark("type_of_recipient_code", FieldStatus.INFERRED, source="dora-roi", note=note)

        provider = providers.get(link.source_key or "")
        if provider is None or not provider.identification_code:
            continue
        link.provider_code = provider.identification_code
        link.provenance["provider_code"] = provider.provenance.get("identification_code") or Provenance(
            status=FieldStatus.INFERRED, source="dora-roi", note="joined from B_05.01"
        )
        if provider.type_of_code:
            link.type_of_code = provider.type_of_code
            link.provenance["type_of_code"] = provider.provenance.get("type_of_code") or Provenance(
                status=FieldStatus.INFERRED, source="dora-roi", note="joined from B_05.01"
            )


# -- output -----------------------------------------------------------------


def _perimeter(sources: _Sources, use_gleif: bool, overlay_file: Path | None = None) -> dict[str, Any]:
    return {
        "state_files": sources.declared,
        "aws": sources.aws,
        # Whether a `--sources` file's `aws:` block named any accounts to
        # sweep at all — distinct from `swept_accounts` being empty, which
        # can also mean a sweep was configured but every account in it was
        # refused. `methodology.to_markdown` needs both to tell "no channel
        # beyond Marketplace was ever attempted" apart from "it was attempted
        # and found nothing".
        "aws_sweep_configured": sources.aws_sweep is not None,
        "kubernetes": sources.k8s_context or sources.k8s,
        "gleif": use_gleif,
        "overlay": str(overlay_file) if overlay_file else None,
        # (account_id, [refusal, ...]) — the exact per-account slice recorded
        # in `_collect_aws`, not reconstructed from `clickops_refused` by
        # string matching. JSON round-trips a tuple as a two-element array.
        "swept_accounts": [(account_id, list(refusals)) for account_id, refusals in sources.swept_accounts],
        # The distinct `resource_types` keys found in one account's own,
        # unmerged evidence — enough for the methodology note to say a
        # channel "found something" without shipping raw provider objects
        # (not JSON-safe: `set`/`Counter` fields) through a structure this
        # module also feeds straight into `json.dumps`. A list, positionally
        # aligned with `swept_accounts` above — never a dict keyed by
        # account_id, which is not unique (R4; see `swept_evidence`'s own
        # docstring on `_Sources`).
        "swept_evidence": [
            sorted({key for provider in providers for key in provider.resource_types})
            for providers in sources.swept_evidence
        ],
        "unreachable_accounts": list(sources.unreachable_accounts),
        "clickops_refused": list(sources.clickops_refused),
        "unnamed_principals": [
            (principal.account_id, principal.role_name, principal.has_external_id)
            for principal in sources.unnamed_principals
        ],
    }


def _inventory(discovered: list[DiscoveredProvider], perimeter: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_by": f"dora-roi {__version__}",
        "perimeter": perimeter,
        "providers": [
            {
                "name": provider.name,
                "namespace": provider.namespace,
                "registry": provider.registry,
                "address": provider.address,
                "resource_count": provider.resource_count,
                "resource_types": dict(sorted(provider.resource_types.items())),
                "regions": sorted(provider.regions),
                "source_files": sorted(provider.source_files),
            }
            for provider in discovered
        ],
    }


def _prefill(roi: RegisterOfInformation, perimeter: dict[str, Any]) -> dict[str, Any]:
    """Every template, read through TEMPLATE_COLLECTIONS rather than by hand.

    This listed four templates until the other eleven got a row model, at
    which point B_01.02 hints and B_02.02 contract detail were being built and
    then silently dropped on the way out. Enumerating the register in one place
    is what stops that happening again.
    """
    templates: dict[str, list[dict[str, Any]]] = {}
    for template, attribute in TEMPLATE_COLLECTIONS.items():
        held = getattr(roi, attribute, None)
        if held is None:
            templates[template] = []
        elif isinstance(held, list):
            templates[template] = [_row_payload(row) for row in held]
        else:
            templates[template] = [_row_payload(held)]
    return {
        "generated_by": f"dora-roi {__version__}",
        "disclaimer": DISCLAIMER,
        "perimeter": perimeter,
        "templates": templates,
    }


def _row_payload(row: RoIRow) -> dict[str, Any]:
    """Values and provenance, both keyed by official field code, plus the join key.

    ``source_key`` sits alongside ``values``/``provenance``, not inside either
    of them: it must never be mistaken for a reportable field (it has no
    alias and no place in a filing), but a reader of ``roi_prefill.json`` —
    and, structurally, this same module's own ``_already_settled`` — needs a
    stable way to match a row back to the provider that produced it. Zipping
    two independently-ordered lists by position is the alternative this
    exists to rule out: this codebase already learned that lesson once, in
    :func:`.overlay.vendors.apply_overlay`.
    """
    code_of = {name: info.alias for name, info in type(row).model_fields.items() if info.alias is not None}
    return {
        "source_key": row.source_key,
        "values": row.model_dump(by_alias=True, mode="json"),
        "provenance": {
            code_of[name]: {"status": str(entry.status), "source": entry.source, "note": entry.note}
            for name, entry in row.provenance.items()
            if name in code_of
        },
    }


def _write(path: Path, content: str) -> None:
    path.write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")


def _print_summary(discovered: list[DiscoveredProvider], summary: Any, output: Path) -> None:
    table = Table(title="Prefill result", title_justify="left")
    table.add_column("")
    table.add_column("", justify="right")
    table.add_row("Providers discovered", str(len(discovered)))
    table.add_row("Fields in the register", str(summary.total))
    table.add_row("[green]Filled[/green]", str(summary.filled))
    table.add_row("[yellow]Inferred (review these)[/yellow]", str(summary.inferred))
    table.add_row("Missing", str(summary.missing))
    table.add_row("[red]Blocking a filing[/red]", str(summary.blocking_missing))
    console.print(table)
    if summary.unprovenanced:
        err.print(
            f"[yellow]Warning:[/yellow] {summary.unprovenanced} field(s) hold a value with no provenance. "
            f"That is a bug in dora-roi, please report it."
        )
    console.print(
        f"\nWritten to [bold]{output}[/bold]: inventory.json, roi_prefill.json, "
        f"gap-report.md, gap-report.json, methodology.md"
    )


#: Console label and one-line explanation for each `refusal_kind()` bucket,
#: in the order printed. Four buckets, never one: a review found the console
#: printing everything under a single "Refused" heading called a trust-policy
#: parse failure and an unreached account "refused" — both AWS said nothing
#: to, and the very `methodology.md` this same run writes says so in as many
#: words (R2). Labelling them apart here is what keeps the two surfaces from
#: contradicting each other on the one claim this whole task exists to keep.
_REFUSAL_HEADINGS: dict[str, tuple[str, str]] = {
    "denied": ("Refused", "AWS said no to a specific action"),
    "parse_failure": ("Could not parse", "dora-roi's own failure, not an AWS denial"),
    "not_reached": ("Named but never reached", "no session could be established"),
    "not_attempted": ("Never attempted", "no account or region was ever known to try"),
    # `refusal_kind`'s own docstring names exactly why this bucket exists:
    # Cost Explorer wraps a missing credential, a denied permission and an
    # unreachable region into one exception, and Marketplace's broadest
    # catch takes whatever else a client or a connection can do. Folding
    # either into "Refused" (a review finding, N1) would claim more than
    # the exception that produced the line distinguishes.
    "unavailable": ("Unavailable", "credentials, connectivity and denials are not told apart here"),
}


def _print_refusals(refused: list[str]) -> None:
    """Every refusal, grouped by what it actually means, never truncated.

    A fixed cap falsifies `PERIMETER_WARNING`'s own claim that a refusal in
    scope "is reported above, by name" the moment a run has more refusals
    than the cap — the same defect shape C2 was, in the same constant, one
    round later (R3). This tool already commits to declaring the whole
    perimeter; a long list is the honest cost of that, not a reason to hide
    part of it.
    """
    if not refused:
        return
    buckets: dict[str, list[str]] = {key: [] for key in _REFUSAL_HEADINGS}
    for line in refused:
        buckets[refusal_kind(line)].append(line)
    for kind, (label, note) in _REFUSAL_HEADINGS.items():
        items = buckets[kind]
        if not items:
            continue
        typer.echo("")
        typer.echo(f"{label} ({len(items)}) — {note}:")
        for line in items:
            typer.echo(f"  - {line}")


def _print_perimeter(sources: _Sources, gleif: bool) -> None:
    """Golden rule 5: say what was scanned, never imply completeness.

    C2: this is the surface every user sees on every run, and `describe()`'s
    per-account counts alone are not enough on it — a run with a refused
    channel must not print only "swept N account(s)" and then a categorical
    warning next to it. The refusals themselves get printed here too, in
    full and correctly labelled (see `_print_refusals`), not filed away in
    methodology.md alone.
    """
    typer.echo("")
    typer.echo("Scanned:")
    for line in sources.describe():
        typer.echo(f"  - {line}")
    typer.echo(f"  - GLEIF enrichment: {'on' if gleif else 'off'}")

    _print_refusals(sources.clickops_refused)

    typer.echo("")
    typer.echo(PERIMETER_WARNING)


def _fail(error: Exception) -> None:
    err.print(f"[red]Error:[/red] {error}")
    raise typer.Exit(EXIT_USER_ERROR) from error


def main() -> None:
    """Console-script entry point (``dora-roi``)."""
    app()


if __name__ == "__main__":
    main()
