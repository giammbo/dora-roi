"""The audit-facing account of how a register was produced."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from dora_roi.models.enums import FieldStatus
from dora_roi.models.templates import RegisterOfInformation, ThirdPartyProvider
from dora_roi.report.gap import build_gap_report
from dora_roi.report.methodology import to_markdown

PERIMETER = {
    "state_files": ["s3://b/env:/production/be/terraform.tfstate"],
    "aws": True,
    "kubernetes": False,
    "gleif": True,
    "overlay": None,
}
WHEN = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)


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
        assert "GLEIF:** consulted" in body

    def test_it_refuses_to_be_read_as_a_compliance_statement(self) -> None:
        assert "not a compliance statement" in flat(note())


class TestWhatWasDeliberatelyNotRead:
    """An auditor who finds the other environments on their own asks what else is missing."""

    def test_exclusions_are_named(self) -> None:
        body = note(excluded=["s3://b/ workspace 'staging' (not selected; this run reads 'production')"])
        assert "workspace 'staging'" in body

    def test_nothing_excluded_says_so_explicitly(self) -> None:
        assert "Nothing was in reach and excluded" in note()

    def test_the_shadow_it_limit_is_stated_either_way(self) -> None:
        assert "does not claim to be complete" in flat(note())
        assert "bought on a card" in flat(note())


class TestMethod:
    def test_it_explains_all_three_discovery_channels(self) -> None:
        body = note()
        assert "Terraform providers" in body and "DNS records" in body and "Kubernetes" in body

    def test_it_names_the_providers_it_excludes_as_non_vendors(self) -> None:
        assert "`random`" in note() and "`archive`" in note()

    def test_it_states_the_gleif_rule_that_decides_filled(self) -> None:
        body = flat(note())
        assert "matches the name that was searched" in body
        assert "search and not an equality test" in body

    def test_it_says_the_domain_data_was_reconciled_against_the_eba_files(self) -> None:
        assert "EBA annotated table layout" in flat(note())


class TestWhatTheRegisterAsserts:
    def test_the_three_bases_are_explained_not_just_counted(self) -> None:
        body = note()
        assert "authoritative source said so" in flat(body)
        assert "Reviewed by a person before filing, or it is a guess" in flat(body)

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
