"""The audit-facing account of how a register was produced."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dora_roi.models.enums import FieldStatus
from dora_roi.models.templates import RegisterOfInformation, ThirdPartyProvider
from dora_roi.report.gap import build_gap_report
from dora_roi.report.methodology import to_markdown

PERIMETER = {
    "state_files": ["s3://b/env:/production/be/terraform.tfstate"],
    "aws": True,
    # False by default: most tests in this file are not about the click-ops
    # sweep at all, and a stray "Accounts swept" section describing an aws:
    # block that resolved to nothing would be noise unrelated to what they
    # test. `_perimeter()` below overrides this for the tests that are.
    "aws_sweep_configured": False,
    "kubernetes": False,
    "gleif": True,
    "overlay": None,
}
WHEN = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)


def _perimeter(
    *,
    swept: list[tuple[str, list[str]]] | None = None,
    evidence: list[list[str]] | None = None,
    unreachable: list[tuple[str, str]] | None = None,
    unnamed: list[tuple[str, str, bool]] | None = None,
    global_refused: list[str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """The perimeter dict in the exact shape `to_markdown` receives from the CLI's
    own `_perimeter()` in `cli.py`.

    `swept` is a list of (account_id, refusal lines recorded during that
    account's own sweep) — a list, not a dict, on purpose: `account_id` is
    not guaranteed unique (two profile-only sweep entries both collapse to
    the literal string "unknown account"), and a round of review found that
    keying evidence by it silently drops one account's findings under the
    other's (R4). `evidence` is positionally aligned with `swept` — the
    `resource_types` keys account *i*'s own unmerged evidence carried
    (`swept_evidence` in cli.py, also a list for the same reason).
    `unreachable` and `global_refused` mirror `_Sources.unreachable_accounts`
    and the whole-run `clickops_refused` list respectively.
    """
    swept = swept or []
    unreachable = unreachable or []
    perimeter = dict(PERIMETER)
    perimeter["swept_accounts"] = [(account_id, list(refusals)) for account_id, refusals in swept]
    perimeter["swept_evidence"] = [list(keys) for keys in (evidence or [])]
    perimeter["unreachable_accounts"] = list(unreachable)
    perimeter["unnamed_principals"] = list(unnamed or [])
    perimeter["clickops_refused"] = list(global_refused or [])
    if swept or unreachable:
        # A sweep obviously was configured if it produced either of these —
        # still overridable below for the tests specifically about the
        # `aws_sweep_configured` flag itself.
        perimeter["aws_sweep_configured"] = True
    perimeter.update(overrides)
    return perimeter


def flat(text: str) -> str:
    """Collapse wrapping, so an assertion tests the prose and not the line breaks."""
    return " ".join(text.split())


def note(**kw) -> str:
    roi = kw.pop("roi", None) or RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="AWS")])
    return to_markdown(
        build_gap_report(roi),
        kw.pop("perimeter", PERIMETER),
        kw.pop("providers", [object()]),
        kw.pop("excluded", ()),
        generated_at=WHEN,
        version="0.1.0",
    )


class TestTheThingsAnAuditorAsksFirst:
    def test_it_is_dated(self) -> None:
        """Every other output omits a timestamp so CI can diff it. This one cannot."""
        assert "2026-03-31 09:00 UTC" in note()

    def test_it_names_the_tool_and_version(self) -> None:
        assert "dora-roi 0.1.0" in note()

    def test_it_lists_every_source_read(self) -> None:
        assert "s3://b/env:/production/be/terraform.tfstate" in note()

    def test_it_says_which_channels_were_used(self) -> None:
        body = note()
        assert "AWS Organizations and Cost Explorer:** read" in body
        assert "Kubernetes:** not read" in body
        assert "GLEIF:** enabled" in body

    def test_the_gleif_and_overlay_lines_report_the_setting_not_the_outcome(self) -> None:
        """G3: `_perimeter` writes `use_gleif` and the overlay's path, and
        neither is touched when `GleifClient()` fails to construct, a lookup
        raises, or an overlay entry names a provider no channel found. Saying
        "consulted", or printing the path bare, reported an outcome off a
        value that only records a setting."""
        body = note(perimeter={**PERIMETER, "gleif": True, "overlay": "vendors.yaml"})
        assert "GLEIF:** consulted" not in body
        assert "GLEIF:** enabled" in body
        assert "Manual overlay:** vendors.yaml supplied" in body

    def test_gleif_and_overlay_still_say_when_they_were_off(self) -> None:
        body = note(perimeter={**PERIMETER, "gleif": False, "overlay": None})
        assert "GLEIF:** not enabled" in body
        assert "Manual overlay:** none supplied" in body

    def test_it_refuses_to_be_read_as_a_compliance_statement(self) -> None:
        assert "not a compliance statement" in flat(note())


class TestWhatWasDeliberatelyNotRead:
    """An auditor who finds the other environments on their own asks what else is missing."""

    def test_exclusions_are_named(self) -> None:
        body = note(excluded=["s3://b/ workspace 'staging' (not selected; this run reads 'production')"])
        assert "workspace 'staging'" in body

    def test_nothing_excluded_says_so_explicitly(self) -> None:
        assert "Nothing in reach was skipped on purpose" in note()

    def test_nothing_excluded_does_not_claim_every_source_was_read(self) -> None:
        """F4: `excluded` is filled by `sources.fetch_sources` alone — a
        remote state this run chose not to fetch. Nothing a channel was
        refused on ever reaches it, so "every source named above was read in
        full" was a claim about outcomes printed off a list of intentions,
        and a run whose every AWS channel was denied printed it verbatim,
        directly above the section naming those denials."""
        body = flat(
            to_markdown(
                [],
                _perimeter(
                    swept=[
                        (
                            "111122223333",
                            [
                                "111122223333 iam-idp: iam:ListSAMLProviders denied (AccessDenied)",
                                "111122223333 iam-idp: iam:ListOpenIDConnectProviders denied (AccessDenied)",
                                "111122223333 trust: iam:ListRoles denied (AccessDenied)",
                            ],
                        )
                    ],
                    global_refused=["cost explorer: NoCredentialsError: Unable to locate credentials"],
                ),
                [],
            )
        )
        assert "was read in full" not in body
        assert "every source named above" not in body
        # The positive half, so this cannot pass by the section falling
        # silent: it still says what the empty list means, and the refusals
        # it used to contradict are still rendered.
        assert "Nothing in reach was skipped on purpose" in body
        assert "Identity providers: refused" in body

    def test_the_section_names_the_three_failures_it_cannot_see(self) -> None:
        """G2: the F4 replacement claimed a failed read is reported with its
        channel. Three are reported nowhere in this document —
        `collect_organization`, `_enrich_with_gleif` (both the client and each
        lookup) and `apply_overlay`'s warnings all print to stderr and put
        nothing in the perimeter dict this module renders from. Recording them
        is a change to the CLI's wiring, tracked separately; until then the
        document names its own blind spot rather than implying it has none."""
        body = flat(note())
        assert "Three are reported nowhere in this document" in body
        for subsystem in ("AWS Organizations", "GLEIF lookup", "overlay entry naming a provider"):
            assert subsystem in body, subsystem
        # And it says what those lines will wrongly read on such a run, so the
        # caveat is checkable against the document above it rather than vague.
        assert "still read *read*, *enabled* and the overlay's own path" in body
        assert "the same run's terminal output is where those three are visible" in body

    def test_the_blind_spot_is_stated_even_when_something_was_excluded(self) -> None:
        """It is a property of the document, not of the empty-`excluded`
        branch — a run that did skip a remote state must not lose it."""
        body = flat(note(excluded=["s3://b/ workspace 'staging' (not selected)"]))
        assert "workspace 'staging'" in body
        assert "Three are reported nowhere in this document" in body

    def test_the_shadow_it_limit_is_stated_either_way(self) -> None:
        assert "does not claim to be complete" in flat(note())
        assert "bought on a card" in flat(note())


class TestMethod:
    def test_it_explains_all_three_discovery_channels(self) -> None:
        body = note()
        assert "Terraform providers" in body and "DNS records" in body and "Kubernetes" in body

    def test_it_names_every_kind_of_evidence_that_can_mint_a_vendor_row(self) -> None:
        """F3: "Vendors were identified from three kinds of evidence, and
        nothing else" outlived four more being added. Marketplace billing,
        identity providers, cross-account trust and EventBridge partner
        sources all mint rows through `cli._fold_in`, and this very document
        lists them two sections earlier under *What was read*."""
        body = flat(note())
        assert "three kinds of evidence, and nothing else" not in body
        for kind in (
            "Terraform providers",
            "DNS records",
            "Kubernetes",
            "AWS Marketplace billing",
            "AWS click-ops discovery",
        ):
            assert f"**{kind}" in body, kind

    def test_it_does_not_read_the_row_minting_rule_as_a_coverage_claim(self) -> None:
        """Golden rule 5: naming every channel that can create a row says
        nothing about whether those channels saw the estate, and the sentence
        must not be readable as if it did."""
        body = flat(note())
        assert "not about coverage" in body
        assert "does not claim to be complete" in body

    def test_it_names_the_providers_it_excludes_as_non_vendors(self) -> None:
        assert "`random`" in note() and "`archive`" in note()

    def test_it_states_the_gleif_rule_that_decides_filled(self) -> None:
        body = flat(note())
        assert "matches the name that was searched" in body
        assert "search and not an equality test" in body

    def test_it_states_the_overlay_half_of_the_gleif_rule_too(self) -> None:
        """`_enrich_with_gleif` writes FILLED only when the match is exact
        **and** the name searched came from the overlay (`cli.py`'s
        `exact and asserted`). Stating the exact-match half alone described
        an exact match on a guessed name as confirmed, which the code has
        never done and — since a Marketplace billing fact can now supply that
        name too — matters more, not less, than when the sentence was written."""
        body = flat(note())
        assert "asserted by a person in the overlay" in body
        assert "recorded as a candidate and marked inferred" in body

    def test_it_says_the_domain_data_was_reconciled_against_the_eba_files(self) -> None:
        assert "EBA annotated table layout" in flat(note())


class TestWhatTheRegisterAsserts:
    def test_the_three_bases_are_explained_not_just_counted(self) -> None:
        body = note()
        assert "authoritative source said so" in flat(body)
        assert "Reviewed by a person before filing, or it is a guess" in flat(body)

    def test_the_inferred_cell_names_every_source_that_writes_inferred(self) -> None:
        """G3: "Derived from a mapping or from infrastructure" named two of
        the five. A GLEIF answer that is not an exact match on an
        overlay-asserted name (`source="gleif"`), a Marketplace figure
        attributed by name (`aws:ce`), and dora-roi's own cross-template join
        (`dora-roi`) are neither a mapping nor infrastructure."""
        body = flat(note())
        assert "Derived from a mapping or from infrastructure." not in body
        for origin in (
            "the packaged mapping",
            "from infrastructure",
            "not an exact match on a name a person asserted",
            "billing figure attributed to a provider by name",
            "dora-roi's own join between templates",
        ):
            assert origin in body, origin

    def test_the_evidence_bullet_does_not_promise_an_account_for_every_channel(self) -> None:
        """G3: Marketplace records `source_files={"aws:ce"}` — a channel with
        no account in it. Only the three per-account channels tag one."""
        body = flat(note())
        assert "the AWS channel and account that named it" not in body
        assert "where the channel is one that sweeps per account" in body

    def test_the_per_template_table_is_there(self) -> None:
        assert "| B_05.01 |" in note()

    def test_a_value_with_no_recorded_basis_is_called_a_defect(self) -> None:
        row = ThirdPartyProvider(legal_name="Set but never marked")
        body = note(roi=RegisterOfInformation(providers=[row]))
        assert "defect in the tool" in flat(body)

    def test_it_names_the_authoritative_sources_actually_relied_on(self) -> None:
        row = ThirdPartyProvider(legal_name="AWS")
        row.mark("legal_name", FieldStatus.FILLED, source="gleif")
        assert "`gleif`" in note(roi=RegisterOfInformation(providers=[row]))


class TestWrittenBesideTheRegister:
    def test_scan_writes_it(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from dora_roi.cli import app

        runner = CliRunner(env={"COLUMNS": "200"})
        sample = Path(__file__).parent / "fixtures" / "sample.tfstate"
        result = runner.invoke(app, ["scan", "-s", str(sample), "-o", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "methodology.md").is_file()

    def test_its_counts_agree_with_the_gap_report(self, tmp_path: Path) -> None:
        """Generated from the same run, so it cannot describe a different register."""
        from typer.testing import CliRunner

        from dora_roi.cli import app

        runner = CliRunner(env={"COLUMNS": "200"})
        sample = Path(__file__).parent / "fixtures" / "sample.tfstate"
        runner.invoke(app, ["scan", "-s", str(sample), "-o", str(tmp_path)])
        summary = json.loads((tmp_path / "gap-report.json").read_text())["summary"]
        body = (tmp_path / "methodology.md").read_text()
        assert f"| Filled | {summary['filled']} |" in body
        assert f"| Missing | {summary['missing']} |" in body


class TestSystemicCaveats:
    """A note that qualifies a whole template belongs in an audit document.

    A note describing one vendor does not, and there are far more of the second
    kind — left in, they bury the first.
    """

    def register(self):
        from dora_roi.models.templates import GroupEntity

        roi = RegisterOfInformation(
            group_entities=[GroupEntity(name=n) for n in ("prod", "staging")],
            providers=[ThirdPartyProvider(legal_name=n) for n in ("AWS", "Datadog")],
        )
        for entity in roi.group_entities:
            entity.mark(
                "name",
                FieldStatus.INFERRED,
                source="aws:organizations",
                note="an AWS account is not a legal entity",
            )
        for provider in roi.providers:
            provider.mark("person_type", FieldStatus.INFERRED, source="mapping", note="assumed: a company")
        # One vendor's own blurb, on one row only.
        roi.providers[0].mark(
            "legal_name", FieldStatus.INFERRED, source="mapping", note="EU customers contract with the Lux entity"
        )
        return roi

    def test_a_caveat_covering_every_row_is_surfaced(self) -> None:
        body = note(roi=self.register(), providers=[object(), object()])
        assert "an AWS account is not a legal entity" in body
        assert "assumed: a company" in body

    def test_a_note_about_one_vendor_is_not(self) -> None:
        body = note(roi=self.register(), providers=[object(), object()])
        assert "Lux entity" not in body

    def test_the_caveat_names_the_template_and_the_source(self) -> None:
        body = note(roi=self.register(), providers=[object(), object()])
        assert "**B_01.02**, from `aws:organizations`" in body

    def test_the_section_is_absent_when_there_is_nothing_to_qualify(self) -> None:
        assert "What the inferred values actually mean" not in note()

    def test_the_b_01_02_caveat_reaches_a_real_scan(self, tmp_path: Path) -> None:
        """The gap this section was added for: 9 AWS accounts under a template
        called "Entities within scope of consolidation"."""
        from typer.testing import CliRunner

        from dora_roi.cli import app
        from dora_roi.collectors.aws import DiscoveredAccount, ExpenseReport, OrganizationInventory

        runner = CliRunner(env={"COLUMNS": "200"})
        sample = Path(__file__).parent / "fixtures" / "sample.tfstate"
        org = OrganizationInventory(
            organization_id="o-x",
            master_account_id="1",
            accounts=[DiscoveredAccount(account_id="1", name="prod", email=None, status="ACTIVE", ou_path=("Root",))],
        )
        import dora_roi.cli as cli_module

        original_org = cli_module.collect_organization
        original_ce = cli_module.collect_annual_expense
        cli_module.collect_organization = lambda **k: org
        cli_module.collect_annual_expense = lambda **k: ExpenseReport(
            currency="EUR", by_service={}, period=(WHEN.date(), WHEN.date())
        )
        try:
            runner.invoke(app, ["scan", "-s", str(sample), "-o", str(tmp_path), "--aws"])
        finally:
            cli_module.collect_organization = original_org
            cli_module.collect_annual_expense = original_ce
        assert "not a legal entity" in (tmp_path / "methodology.md").read_text()


class TestTheHonestySurface:
    """Task 8: a channel that returned nothing and a channel that was refused
    must never render the same. `ListSAMLProviders` returning zero results and
    `ListSAMLProviders` denied by IAM are opposite claims about the world that
    arrive as the identical empty list — the methodology note is the one place
    that is allowed to tell them apart.

    Fix round 1 (a subsequent review): the first cut identified an account by
    parsing it back out of the refusal text, which broke for an account keyed
    by a `role_arn` (C1) and conflated evidence across accounts sharing one
    vendor (C3). Accounts are now identified by construction — see
    `_perimeter`'s `swept`/`evidence` parameters — never reconstructed.

    Fix round 2 (a further review): a residual (unattributed) refusal
    disclosed itself without withdrawing the positive claim standing next to
    it (R1); the console labelled a parse failure and an unreached account
    "Refused" while methodology.md explains neither is one (R2); a fixed
    five-line cap falsified `PERIMETER_WARNING`'s own claim (R3);
    `swept_evidence` keyed by `account_id` silently dropped one account's
    findings under another's when both fell back to "unknown account" (R4);
    and a single denied `list_*` call failed the whole identity-providers
    channel even when the other list call succeeded and found a vendor (R5).
    `swept`/`evidence` are lists now, not dicts, precisely so a duplicate
    account id can be represented and tested (R4).
    """

    def test_an_empty_channel_and_a_refused_channel_do_not_read_the_same(self) -> None:
        read = to_markdown([], _perimeter(swept=[("111122223333", [])]), [])
        denied = to_markdown(
            [],
            _perimeter(
                swept=[
                    (
                        "111122223333",
                        [
                            "111122223333 iam-idp: iam:ListSAMLProviders denied (ClientError: AccessDenied)",
                            "111122223333 iam-idp: iam:ListOpenIDConnectProviders denied (ClientError: AccessDenied)",
                        ],
                    )
                ]
            ),
            [],
        )
        assert read != denied
        assert "no result" in read.lower()
        assert "refused" in denied.lower()
        assert "ListSAMLProviders" in denied
        # I3: the section's own intro paragraph defines both "read, no result"
        # and "refused" in every rendered document, so a bare word-presence
        # check passes even for an implementation that renders every channel
        # identically and dumps the raw refusal in an appendix. Pin the
        # actual rendered line instead.
        assert "Identity providers: read, no result" not in denied
        assert "Identity providers: refused" in denied

    def test_nothing_swept_does_not_pick_one_of_its_two_causes(self) -> None:
        """G3: "configured, but no account was reached" is rendered on two
        different runs — one where accounts resolved and every session failed,
        and one where the `aws:` block resolved to no account at all, which
        never tried to reach anything. *Accounts swept* tells those apart in
        three branches; this line must not pick one of them."""
        never_resolved = flat(to_markdown([], _perimeter(aws_sweep_configured=True), []))
        assert "no account was reached" not in never_resolved
        assert "configured, but nothing was swept" in never_resolved
        # The line that does distinguish is still the one carrying the answer.
        assert "did not resolve to a single account" in never_resolved

    def test_an_outside_principal_says_what_outside_was_subtracted_from(self) -> None:
        """G3: `_collect_aws` passes `own_accounts=frozenset()` when
        Organizations could not be read, and `collect_trust_relationships`
        excludes only `own_accounts` and the swept account itself — so on
        exactly that run a sibling account in the same organisation is
        reported here as if it were a third party."""
        body = flat(
            to_markdown([], _perimeter(swept=[("111122223333", [])], unnamed=[("999988887777", "Mystery", False)]), [])
        )
        assert "decided by subtraction" in body
        assert "a sibling account in your own organisation appears below exactly as a third party would" in body
        # The finding itself is still asserted, not softened away.
        assert "999988887777" in body
        assert "without an `sts:ExternalId` condition" in body

    def test_an_unnamed_external_principal_is_stated_not_hidden(self) -> None:
        note = to_markdown(
            [],
            _perimeter(swept=[("111122223333", [])], unnamed=[("999988887777", "MysteryRole", True)]),
            [],
        )
        assert "999988887777" in note
        assert "MysteryRole" in note
        assert "could not name" in note.lower()

    def test_an_account_that_could_not_be_reached_at_all_is_declared_not_dropped(self) -> None:
        """Session construction can succeed for a profile with dead
        credentials and no `role_arn` — the account never lands in
        `swept_accounts`, but it must not simply vanish either."""
        body = to_markdown(
            [],
            _perimeter(unreachable=[("555566667777", "no usable credentials (ProfileNotFound: bogus)")]),
            [],
        )
        assert "555566667777" in body
        assert "no usable credentials" in body

    def test_a_channel_never_swept_for_lack_of_a_sources_file_says_so(self) -> None:
        """A bare `--aws` scan with no `--sources` file runs Marketplace
        only. The note must not let a reader conclude the other three
        channels — identity providers, trust, EventBridge — were swept."""
        body = to_markdown([], _perimeter(aws_sweep_configured=False), [])
        assert "no `--sources`" in body or "not swept" in body.lower()
        assert "Marketplace" in body

    def test_eventbridge_never_attempted_for_lack_of_a_known_region_says_so(self) -> None:
        """No state file and no cluster means no region was ever known, so
        EventBridge could not run for any swept account — that must be
        declared, not rendered as a silent 'read, no result'."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[("111122223333", [])],
                global_refused=["eventbridge: no regions known (no state file or cluster named one)"],
            ),
            [],
        )
        assert "no region" in body.lower()

    def test_an_eventbridge_refusal_names_its_region(self) -> None:
        """A regional refusal must not lose the one detail — which region —
        that tells an operator which IAM policy to fix."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[
                    (
                        "111122223333",
                        ["111122223333/eu-west-1 eventbridge: events:ListEventSources denied (AccessDenied)"],
                    )
                ]
            ),
            [],
        )
        assert "eu-west-1" in body
        assert "ListEventSources" in body

    def test_an_eventbridge_refusal_names_its_region_even_for_an_arn_keyed_account(self) -> None:
        """C1: an account named only by `role_arn` (no explicit `id`) is keyed
        by the ARN itself, which contains a `/` of its own. Splitting the
        EventBridge refusal on the first `/` — instead of stripping the known
        account id as a literal prefix — used to hand back the wrong region
        (a fragment of the role name) and fail the lookup entirely, silently
        rendering a denied call as a clean empty result."""
        account_id = "arn:aws:iam::123456789012:role/DoraReader"
        body = to_markdown(
            [],
            _perimeter(
                swept=[
                    (
                        account_id,
                        [f"{account_id}/eu-west-1 eventbridge: events:ListEventSources denied (AccessDenied)"],
                    )
                ]
            ),
            [],
        )
        assert "Partner event sources: refused" in body
        assert "Partner event sources: read, no result" not in body
        assert "eu-west-1: events:ListEventSources denied" in body

    def test_no_accounts_resolved_to_sweep_is_not_described_as_a_refused_account(self) -> None:
        """`assume_role_name` with Organizations unreachable resolves to zero
        accounts — nothing was ever named to sweep, which is an earlier and
        different failure than a named account being refused, and the note
        must say which one actually happened."""
        body = to_markdown(
            [],
            _perimeter(
                global_refused=[
                    "aws sweep: assume_role_name is set but AWS Organizations was unreachable, so there is no "
                    "account list to assume it into."
                ],
                aws_sweep_configured=True,
            ),
            [],
        )
        assert "no account list to assume it into" in body
        assert "failed before a usable session existed" not in body

    def test_an_empty_aws_block_is_not_described_as_a_refused_sweep(self) -> None:
        """An `aws:` block with neither `accounts:` nor `assume_role_name:`
        resolves to zero accounts without anything ever being refused — a
        third, milder case than either "every account was refused" or
        "`assume_role_name` never resolved anything", and must not borrow
        either's wording."""
        body = to_markdown([], _perimeter(aws_sweep_configured=True), [])
        assert "did not resolve to a single account" in body
        assert "failed before a usable session existed" not in body
        assert "no account list to assume it into" not in body

    def test_a_channel_that_actually_found_something_is_not_reported_as_no_result(self) -> None:
        """The third outcome: a channel that ran and found a vendor is neither
        an empty result nor a refusal, and collapsing it into 'read, no
        result' would hide that this account produced real evidence."""
        body = to_markdown(
            [],
            _perimeter(swept=[("111122223333", [])], evidence=[["saml_provider"]]),
            [],
        )
        assert "Identity providers: read" in body
        assert "Identity providers: read, no result" not in body

    def test_evidence_from_one_account_never_credits_another(self) -> None:
        """C3: `merge_providers` unions evidence across every account sharing
        one vendor name, which is correct for the register but wrong for this
        note — a vendor found via identity providers in account A and via
        trust in account B must not make account A's own trust line, or
        account B's own identity-providers line, read as "found something"."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[("111122223333", []), ("444455556666", [])],
                evidence=[["saml_provider"], ["assume_role_trust"]],
            ),
            [],
        )
        accounts = body.split("## Accounts swept", 1)[1].split("## External", 1)[0]
        account_a = accounts.split("**111122223333**", 1)[1].split("**444455556666**", 1)[0]
        account_b = accounts.split("**444455556666**", 1)[1]
        assert "Identity providers: read" in account_a
        assert "Cross-account trust: read, no result" in account_a
        assert "Cross-account trust: read" in account_b
        assert "Identity providers: read, no result" in account_b

    def test_two_accounts_that_both_collapse_to_unknown_still_keep_separate_evidence(self) -> None:
        """R4: two profile-only sweep entries with neither `id` nor
        `role_arn` both fall back to the literal string "unknown account" —
        `account_id` is not unique. Evidence keyed by that string in a dict
        would let the second account's findings silently overwrite the
        first's; a list positionally aligned with `swept_accounts` cannot
        collide this way, so both blocks must keep their own findings."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[("unknown account", []), ("unknown account", [])],
                evidence=[["saml_provider"], ["assume_role_trust"]],
            ),
            [],
        )
        accounts = body.split("## Accounts swept", 1)[1].split("## External", 1)[0]
        first, second = accounts.split("**unknown account**")[1:3]
        assert "Identity providers: read" in first
        assert "Cross-account trust: read, no result" in first
        assert "Cross-account trust: read" in second
        assert "Identity providers: read, no result" in second

    def test_an_unattributed_refusal_is_shown_not_dropped(self) -> None:
        """I1/R1: a refusal in a shape this note does not recognise — a fifth
        channel, a reworded message — must not silently leave its channel
        defaulted to a false "read, no result". A review found the first cut
        of this fix disclosed the refusal *and* still made the positive claim
        three lines above it — not two facts side by side, a contradiction.
        Every channel line for this account must withdraw its clean claim
        instead."""
        body = to_markdown(
            [],
            _perimeter(swept=[("111122223333", ["111122223333 config-rules: config:DescribeConfigRules denied"])]),
            [],
        )
        assert "config:DescribeConfigRules denied" in body
        assert "could not attribute to a channel" in body.lower()
        # R1: none of the three channels may make the bare positive claim
        # anymore — the unattributed refusal might belong to any of them.
        assert "Identity providers: read, no result" not in body
        assert "Cross-account trust: read, no result" not in body
        assert "Partner event sources: read, no result" not in body
        assert body.count("unconfirmed —") == 3

    def test_an_unattributed_refusal_does_not_overwrite_a_channels_own_known_state(self) -> None:
        """R1's fix must not overreach: a channel that already carries its
        own refused/partial state says something concrete already happened,
        and is not the line the unattributed refusal could be hiding behind."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[
                    (
                        "111122223333",
                        [
                            "111122223333 trust: iam:ListRoles denied (AccessDenied)",
                            "111122223333 config-rules: config:DescribeConfigRules denied",
                        ],
                    )
                ]
            ),
            [],
        )
        assert "Cross-account trust: refused — iam:ListRoles denied" in body
        assert "Cross-account trust: unconfirmed" not in body
        # The other two channels have no refusal of their own, so they do get
        # withdrawn — only the one with its own known state is left alone.
        assert "Identity providers: unconfirmed" in body
        assert "Partner event sources: unconfirmed" in body

    def test_a_per_item_idp_denial_does_not_fail_the_whole_channel(self) -> None:
        """I2: `GetSAMLProvider` denied on one already-listed ARN, after
        `ListSAMLProviders` itself succeeded, costs one provider's document —
        not the channel. Rendering it as a whole-channel refusal would
        contradict a register that still carries rows this channel found."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[
                    (
                        "111122223333",
                        [
                            "111122223333 iam-idp: iam:GetSAMLProvider on "
                            "arn:aws:iam::111122223333:saml-provider/okta (ClientError: AccessDenied)"
                        ],
                    )
                ],
                evidence=[["saml_provider"]],
            ),
            [],
        )
        assert "Identity providers: refused" not in body
        assert "Identity providers: read, but could not fully enumerate this account's identity providers" in body
        assert "GetSAMLProvider" in body

    def test_one_list_call_denied_while_the_other_succeeds_is_not_a_whole_channel_refusal(self) -> None:
        """R5: I2's own defect, one call pair over. `ListSAMLProviders`
        denied while `ListOpenIDConnectProviders` succeeded — and found a
        vendor — must not render the whole "Identity providers" channel a
        refusal next to the register row that surviving call produced."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[
                    (
                        "111122223333",
                        ["111122223333 iam-idp: iam:ListSAMLProviders denied (ClientError: AccessDenied)"],
                    )
                ],
                evidence=[["oidc_provider"]],
            ),
            [],
        )
        assert "Identity providers: refused" not in body
        assert "Identity providers: read, but could not fully enumerate this account's identity providers" in body
        assert "ListSAMLProviders denied" in body

    def test_both_list_calls_denied_is_a_genuine_whole_channel_refusal(self) -> None:
        """The R5 fix must not blur the other direction: both list calls
        denied really did fail the whole channel, and must still say so."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[
                    (
                        "111122223333",
                        [
                            "111122223333 iam-idp: iam:ListSAMLProviders denied (AccessDenied)",
                            "111122223333 iam-idp: iam:ListOpenIDConnectProviders denied (AccessDenied)",
                        ],
                    )
                ]
            ),
            [],
        )
        assert "Identity providers: refused" in body
        assert "could not fully enumerate" not in body

    def test_an_unparseable_trust_policy_is_not_called_an_aws_refusal(self) -> None:
        """I2: "unreadable trust policy on role 'X'" is dora-roi's own parse
        failure — AWS never said no to anything. The section defines
        *refused* as "AWS said no to a specific action"; labelling a local
        parse failure that way asserts something that did not happen."""
        body = to_markdown(
            [],
            _perimeter(swept=[("111122223333", ["111122223333 trust: unreadable trust policy on role 'Legacy'"])]),
            [],
        )
        assert "Cross-account trust: refused" not in body
        assert "Cross-account trust: read, but could not parse 1 role" in body
        assert "not an AWS denial" in body
        assert "unreadable trust policy on role 'Legacy'" in body

    def test_a_whole_channel_list_denial_still_renders_as_refused(self) -> None:
        """The I2 fix must not blur the other direction: `ListRoles` itself
        denied is a genuine, whole-channel AWS refusal."""
        body = to_markdown(
            [],
            _perimeter(swept=[("111122223333", ["111122223333 trust: iam:ListRoles denied (AccessDenied)"])]),
            [],
        )
        assert "Cross-account trust: refused — iam:ListRoles denied" in body

    def test_refused_is_not_glossed_as_aws_saying_no(self) -> None:
        """F2: every producer of a per-channel refusal is an
        `except Exception` — `clickops.py`'s `_listed` and `_denial`, and the
        `ListRoles` and `ListEventSources` catches. A profile-only sweep
        account is never validated until its first call, so a
        `NoCredentialsError` renders here as *refused*, and three of the five
        producers write the word "denied" into the message before looking at
        the exception at all. The intro may not gloss that as AWS having said
        no to a specific action."""
        body = flat(to_markdown([], _perimeter(swept=[("111122223333", [])]), []))
        assert "AWS said no to a specific action" not in body
        # The positive half: the state is still defined, and still defined as
        # the opposite of an empty result — the distinction this note exists
        # to keep is not what was hedged.
        assert "came back an error instead of an answer" in body
        assert "named for what dora-roi saw, not for what AWS did" in body
        assert "the same empty list in memory and are opposite claims" in body

    def test_a_credential_failure_renders_refused_which_is_why_the_gloss_is_hedged(self) -> None:
        """The code path behind F2, rendered: `boto3.Session()` for a
        profile with no usable credentials constructs fine (nothing is
        validated until the first call), so both `list_*` calls fail with
        `NoCredentialsError` and this account renders *refused* — no AWS
        denial anywhere in it."""
        body = to_markdown(
            [],
            _perimeter(
                swept=[
                    (
                        "111122223333",
                        [
                            "111122223333 iam-idp: iam:ListSAMLProviders denied "
                            "(NoCredentialsError: Unable to locate credentials)",
                            "111122223333 iam-idp: iam:ListOpenIDConnectProviders denied "
                            "(NoCredentialsError: Unable to locate credentials)",
                        ],
                    )
                ]
            ),
            [],
        )
        assert "Identity providers: refused" in body
        assert "Identity providers: read, no result" not in body
        # The exception type is the only thing that tells this from a denial,
        # so it has to survive into the rendered line.
        assert "NoCredentialsError" in body

    def test_the_residual_heading_does_not_speak_for_the_whole_run(self) -> None:
        """ "Every other refusal above was recognised as belonging to one of
        the three channels" covered more than the classifier ever sees: the
        whole-run refusals (Cost Explorer, Marketplace, an unresolved sweep,
        a channel with no known region) belong to no account and are never
        put through `_classify_refusals` at all."""
        body = flat(
            to_markdown(
                [],
                _perimeter(
                    swept=[("111122223333", ["111122223333 config-rules: config:DescribeConfigRules denied"])],
                    global_refused=["cost explorer: NoCredentialsError: Unable to locate credentials"],
                ),
                [],
            )
        )
        assert "Every other refusal above was recognised" not in body
        assert "belong to no single account and are not classified here at all" in body
        # The positive half: the residual itself is still surfaced verbatim.
        assert "config:DescribeConfigRules denied" in body

    def test_every_account_unreachable_is_not_described_as_refused(self) -> None:
        """An account whose session never came up was refused nothing: no
        call was made. The fallback text called it "refused before a usable
        session existed" while every other surface in this run — the console
        buckets, `refusal_kind`, the per-account table — keeps the two
        apart."""
        body = flat(
            to_markdown(
                [],
                _perimeter(unreachable=[("555566667777", "no usable credentials (ProfileNotFound: bogus)")]),
                [],
            )
        )
        assert "refused before a usable session existed" not in body
        assert "failed before a usable session existed" in body
        assert "none of the three channels above ran in any of them" in body

    def test_a_session_that_never_came_up_is_not_described_as_no_call_at_all(self) -> None:
        """G1: `_session` issues `sts:AssumeRole` *before* a session exists,
        and `_sweep_accounts` synthesises a `role_arn` for every organisation
        account whenever `assume_role_name` is set — so the ordinary failure
        on this path is AWS refusing that call. "No call was made in any of
        them" sat three lines above the quoted AccessDenied AWS returned to
        the call that was made: this task's own conflation, inverted."""
        body = flat(
            to_markdown(
                [],
                _perimeter(
                    unreachable=[
                        (
                            "555566667777",
                            "no usable credentials (could not assume "
                            "arn:aws:iam::555566667777:role/DoraReader: AccessDenied)",
                        )
                    ]
                ),
                [],
            )
        )
        assert "no call was made" not in body
        assert "not the same as nothing having been attempted" in body
        assert "which AWS can refuse" in body
        # The denial it would have contradicted is still rendered, verbatim,
        # below the sentence that now allows for it.
        assert "could not assume arn:aws:iam::555566667777:role/DoraReader: AccessDenied" in body


class TestRefusedChannels:
    """F1: `describe()`'s "N with at least one channel refused" counted
    refusal *lines* of kind `"denied"`, while methodology.md decides per
    channel whether the account was refused at all. One denied
    `iam:ListSAMLProviders` satisfied the first and not the second, so the
    terminal and the document written by the same run contradicted each
    other. `refused_channels` is the one verdict both now read."""

    def test_a_single_list_denial_does_not_refuse_the_channel(self) -> None:
        from dora_roi.report.methodology import refused_channels

        line = "111122223333 iam-idp: iam:ListSAMLProviders denied (ClientError: AccessDenied)"
        assert refused_channels("111122223333", [line]) == []

    def test_both_list_denials_refuse_the_channel(self) -> None:
        from dora_roi.report.methodology import refused_channels

        lines = [
            "111122223333 iam-idp: iam:ListSAMLProviders denied (ClientError: AccessDenied)",
            "111122223333 iam-idp: iam:ListOpenIDConnectProviders denied (ClientError: AccessDenied)",
        ]
        assert refused_channels("111122223333", lines) == ["Identity providers"]

    def test_a_per_item_get_denial_does_not_refuse_the_channel(self) -> None:
        from dora_roi.report.methodology import refused_channels

        line = (
            "111122223333 iam-idp: iam:GetSAMLProvider on "
            "arn:aws:iam::111122223333:saml-provider/okta (ClientError: AccessDenied)"
        )
        assert refused_channels("111122223333", [line]) == []

    def test_a_parse_failure_does_not_refuse_the_channel(self) -> None:
        from dora_roi.report.methodology import refused_channels

        line = "111122223333 trust: unreadable trust policy on role 'Legacy'"
        assert refused_channels("111122223333", [line]) == []

    def test_listroles_and_eventbridge_denials_do_refuse_their_channels(self) -> None:
        from dora_roi.report.methodology import refused_channels

        lines = [
            "111122223333 trust: iam:ListRoles denied (ClientError: AccessDenied)",
            "111122223333/eu-west-1 eventbridge: events:ListEventSources denied (ClientError: AccessDenied)",
        ]
        assert refused_channels("111122223333", lines) == ["Cross-account trust", "Partner event sources"]

    def test_an_unattributed_refusal_is_not_counted_as_a_refused_channel(self) -> None:
        """It renders each line *unconfirmed*, which is a different claim
        from refused — the count must not borrow the stronger word."""
        from dora_roi.report.methodology import refused_channels

        assert refused_channels("111122223333", ["111122223333 config-rules: config:DescribeConfigRules denied"]) == []

    def test_an_arn_keyed_account_still_finds_its_eventbridge_refusal(self) -> None:
        """C1's root cause, guarded on this path too: a role ARN carries a
        `/` of its own, and EventBridge packs `<account>/<region>` ahead of
        its marker."""
        from dora_roi.report.methodology import refused_channels

        account = "arn:aws:iam::123456789012:role/DoraReader"
        line = f"{account}/eu-west-1 eventbridge: events:ListEventSources denied (AccessDenied)"
        assert refused_channels(account, [line]) == ["Partner event sources"]


class TestRefusalKind:
    """`refusal_kind` is the one place both the console (`cli._print_perimeter`)
    and this module classify a `clickops_refused` line — a review found the
    two surfaces disagreeing (R2) when each grew its own copy of this logic."""

    def test_no_usable_credentials_is_not_reached_not_refused(self) -> None:
        from dora_roi.report.methodology import refusal_kind

        assert refusal_kind("555566667777: no usable credentials (ProfileNotFound: bogus)") == "not_reached"

    def test_unreadable_trust_policy_is_a_parse_failure_not_refused(self) -> None:
        from dora_roi.report.methodology import refusal_kind

        assert refusal_kind("111122223333 trust: unreadable trust policy on role 'Legacy'") == "parse_failure"

    def test_no_regions_known_is_not_attempted_not_refused(self) -> None:
        from dora_roi.report.methodology import refusal_kind

        line = (
            "eventbridge: no regions known (no state file or cluster named one), so partner event "
            "sources were not swept in any account."
        )
        assert refusal_kind(line) == "not_attempted"

    def test_assume_role_name_unresolved_is_not_attempted_not_refused(self) -> None:
        from dora_roi.report.methodology import refusal_kind

        line = (
            "aws sweep: assume_role_name is set but AWS Organizations was unreachable, so there is no "
            "account list to assume it into."
        )
        assert refusal_kind(line) == "not_attempted"

    def test_a_genuine_aws_denial_is_denied(self) -> None:
        from dora_roi.report.methodology import refusal_kind

        assert refusal_kind("111122223333 iam-idp: iam:ListSAMLProviders denied (AccessDenied)") == "denied"
        assert refusal_kind("111122223333 trust: iam:ListRoles denied (AccessDenied)") == "denied"

    def test_cost_explorer_and_marketplace_failures_are_unavailable_not_denied(self) -> None:
        """N1: `_collect_aws`'s own comment on its Cost Explorer catch says
        the wrapped `AwsError` covers a missing credential, a denied
        permission, and an unreachable region as one exception; Marketplace's
        broadest catch (`except Exception`) takes whatever else a client or a
        connection can do. Neither message names an AWS denial specifically,
        so neither may render under the same heading as one."""
        from dora_roi.report.methodology import refusal_kind

        assert refusal_kind("cost explorer: NoCredentialsError: Unable to locate credentials") == "unavailable"
        assert refusal_kind("cost explorer: AwsError: could not reach the Cost Explorer endpoint") == "unavailable"
        assert refusal_kind("marketplace: ce:GetCostAndUsage unavailable (ConnectionError: timed out)") == "unavailable"
