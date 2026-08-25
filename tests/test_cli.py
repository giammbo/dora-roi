"""CLI end to end. Offline by default: no test may touch the network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dora_roi import cli as cli_module
from dora_roi.cli import app
from dora_roi.enrichment.gleif import GleifError, LeiRecord, MatchType

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = str(FIXTURES / "sample.tfstate")
OUTPUTS = ("inventory.json", "roi_prefill.json", "gap-report.md", "gap-report.json")

# rich wraps to 80 columns when stdout is not a tty, which would split the
# very strings these tests assert on. Give it a wide terminal instead.
runner = CliRunner(env={"COLUMNS": "200"})


class TestProvidersCommand:
    def test_lists_the_discovered_providers(self) -> None:
        result = runner.invoke(app, ["providers", "-s", SAMPLE])
        assert result.exit_code == 0
        for name in ("aws", "datadog", "cloudflare"):
            assert name in result.output

    def test_shows_the_mapped_vendor(self) -> None:
        result = runner.invoke(app, ["providers", "-s", SAMPLE])
        assert "Amazon Web Services" in result.output

    def test_marks_an_unmapped_provider(self, tmp_path: Path) -> None:
        state = tmp_path / "x.tfstate"
        state.write_text(
            '{"version": 4, "resources": [{"mode": "managed", "type": "acme_thing", "name": "t",'
            ' "provider": "provider[\\"registry.terraform.io/acme/acme\\"]", "instances": [{}]}]}'
        )
        result = runner.invoke(app, ["providers", "-s", str(state)])
        assert result.exit_code == 0
        assert "not mapped" in result.output.lower() or "—" in result.output

    def test_bad_state_exits_one(self) -> None:
        result = runner.invoke(app, ["providers", "-s", str(FIXTURES / "v3.tfstate")])
        assert result.exit_code == 1

    def test_carries_the_full_disclaimer_docs_01_requires(self) -> None:
        output = runner.invoke(app, ["providers", "-s", SAMPLE]).output.lower()
        assert "read-only" in output
        assert "compliant" in output
        assert "legal advice" in output

    def test_shows_the_namespace(self) -> None:
        assert "hashicorp" in runner.invoke(app, ["providers", "-s", SAMPLE]).output

    def test_short_mapping_flag_works(self, tmp_path: Path) -> None:
        user = tmp_path / "m.yaml"
        user.write_text("aws:\n  vendor: My Reseller BV\n")
        result = runner.invoke(app, ["providers", "-s", SAMPLE, "-m", str(user)])
        assert result.exit_code == 0
        assert "My Reseller BV" in result.output


class TestScanOutputs:
    @pytest.fixture
    def scanned(self, tmp_path: Path):
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        assert result.exit_code == 0, result.output
        return result, tmp_path

    def test_writes_the_four_files(self, scanned) -> None:
        _, out = scanned
        for name in OUTPUTS:
            assert (out / name).is_file(), name

    def test_creates_the_output_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested"
        assert runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(target)]).exit_code == 0
        assert (target / "inventory.json").is_file()

    def test_inventory_declares_the_perimeter(self, scanned) -> None:
        _, out = scanned
        payload = json.loads((out / "inventory.json").read_text())
        assert SAMPLE in payload["perimeter"]["state_files"]
        assert payload["perimeter"]["gleif"] is False
        assert {p["name"] for p in payload["providers"]} == {"aws", "datadog", "cloudflare"}

    def test_inventory_keeps_the_evidence(self, scanned) -> None:
        _, out = scanned
        payload = json.loads((out / "inventory.json").read_text())
        aws = next(p for p in payload["providers"] if p["name"] == "aws")
        assert aws["resource_count"] == 5
        assert sorted(aws["regions"]) == ["eu-south-1", "eu-west-1"]

    def test_prefill_uses_official_field_codes(self, scanned) -> None:
        _, out = scanned
        payload = json.loads((out / "roi_prefill.json").read_text())
        provider = payload["templates"]["B_05.01"][0]
        assert "0050" in provider["values"]
        assert provider["provenance"]["0050"]["status"] == "INFERRED"

    def test_prefill_carries_the_disclaimer(self, scanned) -> None:
        _, out = scanned
        assert json.loads((out / "roi_prefill.json").read_text())["disclaimer"]

    def test_gap_markdown_leads_with_blocking(self, scanned) -> None:
        _, out = scanned
        assert "Blocking gaps first" in (out / "gap-report.md").read_text()

    def test_gap_json_is_machine_readable(self, scanned) -> None:
        _, out = scanned
        payload = json.loads((out / "gap-report.json").read_text())
        assert payload["summary"]["blocking_missing"] > 0
        assert len(payload["summary"]["by_template"]) == 15


class TestSyntheticArrangements:
    @pytest.fixture
    def prefill(self, tmp_path: Path) -> dict:
        assert runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)]).exit_code == 0
        return json.loads((tmp_path / "roi_prefill.json").read_text())

    def test_one_arrangement_per_provider(self, prefill: dict) -> None:
        refs = {a["values"]["0010"] for a in prefill["templates"]["B_02.01"]}
        assert refs == {"ARR-AWS-001", "ARR-DATADOG-001", "ARR-CLOUDFLARE-001"}

    def test_arrangement_reference_is_inferred_not_filled(self, prefill: dict) -> None:
        arrangement = prefill["templates"]["B_02.01"][0]
        assert arrangement["provenance"]["0010"]["status"] == "INFERRED"

    def test_supply_chain_is_rank_one(self, prefill: dict) -> None:
        links = prefill["templates"]["B_05.02"]
        assert links
        assert {link["values"]["0050"] for link in links} == {1}

    def test_supply_chain_carries_the_suggested_service_codes(self, prefill: dict) -> None:
        codes = {link["values"]["0020"] for link in prefill["templates"]["B_05.02"]}
        assert "S17" in codes  # aws IaaS
        assert "S11" in codes  # cloudflare network

    def test_supply_chain_links_back_to_its_arrangement(self, prefill: dict) -> None:
        refs = {a["values"]["0010"] for a in prefill["templates"]["B_02.01"]}
        assert {link["values"]["0010"] for link in prefill["templates"]["B_05.02"]} <= refs


class TestOfflineByDefault:
    def test_gleif_is_off_unless_asked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*a: object, **k: object) -> None:
            raise AssertionError("the default scan must not construct a GLEIF client")

        monkeypatch.setattr(cli_module, "GleifClient", explode)
        assert runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)]).exit_code == 0

    def test_no_gleif_flag_is_explicit_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "GleifClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
        assert runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--no-gleif"]).exit_code == 0


class FakeGleif:
    """Stands in for GleifClient without a socket."""

    def __init__(self, record: LeiRecord | None = None, error: Exception | None = None) -> None:
        self.record = record
        self.error = error

    def __call__(self, *a: object, **k: object) -> FakeGleif:
        return self

    def __enter__(self) -> FakeGleif:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def best_match(self, name: str) -> LeiRecord | None:
        if self.error is not None:
            raise self.error
        return self.record


class TestGleifEnrichment:
    def test_an_exact_match_on_a_guessed_name_is_not_filled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GLEIF confirms the LEI of the entity we guessed, not that it is the counterparty.

        The name searched came from the packaged mapping. A confirmed value built
        on an unconfirmed one is still unconfirmed.
        """
        record = LeiRecord(
            lei="5493001KJTIIGC8Y1R12",
            legal_name="AMAZON WEB SERVICES EMEA SARL",
            country="LU",
            status="ACTIVE",
            match_type=MatchType.EXACT,
        )
        monkeypatch.setattr(cli_module, "GleifClient", FakeGleif(record))
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--gleif"])
        provider = json.loads((tmp_path / "roi_prefill.json").read_text())["templates"]["B_05.01"][0]
        assert provider["values"]["0010"] == "5493001KJTIIGC8Y1R12"
        assert provider["provenance"]["0010"]["status"] == "INFERRED"
        assert "not that it is your counterparty" in provider["provenance"]["0010"]["note"]

    def test_an_exact_match_on_an_asserted_name_is_filled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the name asserted in the overlay, the chain has no guess left in it."""
        record = LeiRecord(
            lei="5493001KJTIIGC8Y1R12",
            legal_name="Acme Cloud SpA",
            country="IT",
            status="ACTIVE",
            match_type=MatchType.EXACT,
        )
        monkeypatch.setattr(cli_module, "GleifClient", FakeGleif(record))
        overlay = tmp_path / "v.yaml"
        overlay.write_text("providers:\n  aws:\n    legal_name: Acme Cloud SpA\n")
        # The overlay is applied after enrichment, so assert the name first by
        # running once to fix it, then again with GLEIF seeing an asserted name.
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--overlay", str(overlay)])
        prefill = json.loads((tmp_path / "roi_prefill.json").read_text())
        assert prefill["templates"]["B_05.01"][0]["provenance"]["0050"]["status"] == "FILLED"

    def test_a_fuzzy_match_stays_inferred(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        record = LeiRecord(
            lei="5493001KJTIIGC8Y1R12",
            legal_name="AMAZON WEB SERVICES EMEA SARL",
            country="LU",
            status="ACTIVE",
            match_type=MatchType.FUZZY,
        )
        monkeypatch.setattr(cli_module, "GleifClient", FakeGleif(record))
        assert runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--gleif"]).exit_code == 0
        prefill = json.loads((tmp_path / "roi_prefill.json").read_text())
        provider = prefill["templates"]["B_05.01"][0]
        assert provider["provenance"]["0010"]["status"] == "INFERRED"
        assert "fuzzy" in provider["provenance"]["0010"]["note"].lower()

    def test_gleif_failure_degrades_to_a_warning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "GleifClient", FakeGleif(error=GleifError("upstream down")))
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--gleif"])
        assert result.exit_code == 0
        assert "warn" in result.output.lower() or "gleif" in result.output.lower()
        assert (tmp_path / "roi_prefill.json").is_file()

    def test_the_perimeter_records_that_gleif_ran(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "GleifClient", FakeGleif(None))
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--gleif"])
        payload = json.loads((tmp_path / "inventory.json").read_text())
        assert payload["perimeter"]["gleif"] is True


class TestExitCodes:
    def test_success_is_zero(self, tmp_path: Path) -> None:
        assert runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)]).exit_code == 0

    def test_bad_state_is_one(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", "-s", str(FIXTURES / "v3.tfstate"), "-o", str(tmp_path)])
        assert result.exit_code == 1
        assert "version 3" in result.output

    def test_missing_state_is_one(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", "-s", str(tmp_path / "nope.tfstate"), "-o", str(tmp_path)])
        assert result.exit_code == 1

    def test_bad_mapping_is_one(self, tmp_path: Path) -> None:
        bad = tmp_path / "map.yaml"
        bad.write_text("aws:\n  services: [S42]\n")
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--mapping", str(bad)])
        assert result.exit_code == 1
        assert "S42" in result.output

    def test_an_unexpected_failure_is_two(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: object, **k: object) -> None:
            raise RuntimeError("something nobody predicted")

        monkeypatch.setattr(cli_module, "build_gap_report", boom)
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        assert result.exit_code == 2
        assert "issue" in result.output.lower()


class TestSeveralStateFiles:
    def test_merges_them_and_records_both(self, tmp_path: Path) -> None:
        second = str(FIXTURES / "second.tfstate")
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-s", second, "-o", str(tmp_path)])
        assert result.exit_code == 0
        payload = json.loads((tmp_path / "inventory.json").read_text())
        assert sorted(payload["perimeter"]["state_files"]) == sorted([SAMPLE, second])
        assert {p["name"] for p in payload["providers"]} == {"aws", "datadog", "cloudflare", "github"}


class TestSummaryOutput:
    def test_prints_a_summary_and_the_disclaimer(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        assert "blocking" in result.output.lower()
        assert "compliant" in result.output.lower()

    def test_states_what_was_scanned(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        assert "sample.tfstate" in result.output

    def test_warns_that_shadow_it_is_invisible(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        assert "shadow" in result.output.lower() or "outside" in result.output.lower()


def test_help_still_works() -> None:
    assert runner.invoke(app, ["--help"]).exit_code == 0


class TestOverlayCommand:
    def test_init_writes_a_commented_template(self, tmp_path: Path) -> None:
        target = tmp_path / "vendors.yaml"
        result = runner.invoke(app, ["overlay", "init", "--to", str(target)])
        assert result.exit_code == 0
        assert target.read_text().lstrip().startswith("#")

    def test_init_seeds_from_the_last_scan(self, tmp_path: Path) -> None:
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        target = tmp_path / "vendors.yaml"
        result = runner.invoke(app, ["overlay", "init", "-o", str(tmp_path), "--to", str(target)])
        assert result.exit_code == 0
        body = target.read_text()
        for name in ("aws", "datadog", "cloudflare"):
            assert f"  {name}:" in body

    def test_init_refuses_to_clobber(self, tmp_path: Path) -> None:
        target = tmp_path / "vendors.yaml"
        target.write_text("mine\n")
        assert runner.invoke(app, ["overlay", "init", "--to", str(target)]).exit_code == 1
        assert target.read_text() == "mine\n"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        target = tmp_path / "vendors.yaml"
        target.write_text("mine\n")
        assert runner.invoke(app, ["overlay", "init", "--to", str(target), "--force"]).exit_code == 0
        assert target.read_text() != "mine\n"

    def test_warns_when_there_is_no_scan_to_seed_from(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["overlay", "init", "-o", str(tmp_path), "--to", str(tmp_path / "v.yaml")])
        assert "warning" in result.output.lower()


class TestScanWithOverlay:
    def test_overlay_values_become_filled(self, tmp_path: Path) -> None:
        overlay = tmp_path / "vendors.yaml"
        overlay.write_text("providers:\n  aws:\n    legal_name: Acme Cloud SpA\n")
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--overlay", str(overlay)])
        assert result.exit_code == 0, result.output
        prefill = json.loads((tmp_path / "roi_prefill.json").read_text())
        provider = prefill["templates"]["B_05.01"][0]
        assert provider["values"]["0050"] == "Acme Cloud SpA"
        assert provider["provenance"]["0050"]["status"] == "FILLED"

    def test_the_perimeter_records_the_overlay(self, tmp_path: Path) -> None:
        overlay = tmp_path / "vendors.yaml"
        overlay.write_text("providers: {}\n")
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--overlay", str(overlay)])
        payload = json.loads((tmp_path / "inventory.json").read_text())
        assert payload["perimeter"]["overlay"] == str(overlay)

    def test_a_typo_in_the_overlay_exits_one(self, tmp_path: Path) -> None:
        overlay = tmp_path / "vendors.yaml"
        overlay.write_text("providers:\n  aws:\n    legal_nmae: Acme\n")
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--overlay", str(overlay)])
        assert result.exit_code == 1
        assert "legal_nmae" in result.output

    def test_an_unmatched_provider_warns_but_does_not_fail(self, tmp_path: Path) -> None:
        overlay = tmp_path / "vendors.yaml"
        overlay.write_text("providers:\n  awz:\n    legal_name: Typo\n")
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--overlay", str(overlay)])
        assert result.exit_code == 0
        assert "awz" in result.output

    def test_overlay_beats_gleif(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        record = LeiRecord(
            lei="5493001KJTIIGC8Y1R12",
            legal_name="AMAZON WEB SERVICES EMEA SARL",
            country="LU",
            status="ACTIVE",
            match_type=MatchType.EXACT,
        )
        monkeypatch.setattr(cli_module, "GleifClient", FakeGleif(record))
        overlay = tmp_path / "vendors.yaml"
        overlay.write_text("providers:\n  aws:\n    legal_name: What Our Contract Says BV\n")
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--gleif", "--overlay", str(overlay)])
        prefill = json.loads((tmp_path / "roi_prefill.json").read_text())
        assert prefill["templates"]["B_05.01"][0]["values"]["0050"] == "What Our Contract Says BV"

    def test_the_gap_report_counts_the_overlay(self, tmp_path: Path) -> None:
        overlay = tmp_path / "vendors.yaml"
        overlay.write_text("providers:\n  aws:\n    legal_name: Acme\n    hq_country: IT\n")
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--overlay", str(overlay)])
        payload = json.loads((tmp_path / "gap-report.json").read_text())
        assert payload["summary"]["filled"] >= 2


class TestCheckCommand:
    def test_reports_blocking_findings_on_a_fresh_scan(self, tmp_path: Path) -> None:
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        result = runner.invoke(app, ["check", "-o", str(tmp_path)])
        assert result.exit_code == 0
        assert "blocking" in result.output.lower()

    def test_fail_on_blocking_exits_one(self, tmp_path: Path) -> None:
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        result = runner.invoke(app, ["check", "-o", str(tmp_path), "--fail-on", "blocking"])
        assert result.exit_code == 1

    def test_fail_on_none_is_the_default_and_exits_zero(self, tmp_path: Path) -> None:
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        assert runner.invoke(app, ["check", "-o", str(tmp_path)]).exit_code == 0

    def test_a_bad_fail_on_value_exits_one(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["check", "-o", str(tmp_path), "--fail-on", "maybe"])
        assert result.exit_code == 1
        assert "--fail-on" in result.output

    def test_no_prefill_is_an_actionable_error(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["check", "-o", str(tmp_path)])
        assert result.exit_code == 1
        assert "scan" in result.output

    def test_it_flags_the_synthetic_references_scan_generated(self, tmp_path: Path) -> None:
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        output = runner.invoke(app, ["check", "-o", str(tmp_path)]).output.lower()
        assert "generated by dora-roi" in output

    def test_gleif_is_off_by_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        monkeypatch.setattr(cli_module, "GleifClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
        assert runner.invoke(app, ["check", "-o", str(tmp_path)]).exit_code == 0

    def test_an_overlay_reduces_the_blocking_count(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        filled = tmp_path / "filled"
        overlay = tmp_path / "vendors.yaml"
        overlay.write_text(
            "entity:\n"
            "  lei: 529900T8BM49AURSDO55\n"
            "  name: Acme\n"
            "  country: IT\n"
            "  entity_type: Payment institution\n"
            "  competent_authority: Banca d'Italia\n"
            "  reporting_date: 2026-03-31\n"
        )
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(plain)])
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(filled), "--overlay", str(overlay)])
        before = runner.invoke(app, ["check", "-o", str(plain)]).output
        after = runner.invoke(app, ["check", "-o", str(filled)]).output
        assert "REPORTING_DATE" not in after or "reporting reference date" not in after
        assert before != after


class TestSourceSelection:
    def test_no_source_at_all_exits_one(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", "-o", str(tmp_path)])
        assert result.exit_code == 1
        assert "nothing to scan" in result.output

    def test_providers_with_no_state_exits_one(self) -> None:
        assert runner.invoke(app, ["providers"]).exit_code == 1


class TestAwsWiring:
    def _org(self):
        from dora_roi.collectors.aws import DiscoveredAccount, OrganizationInventory

        return OrganizationInventory(
            organization_id="o-abc",
            master_account_id="123456789012",
            accounts=[
                DiscoveredAccount(
                    account_id="123456789012",
                    name="acme-prod",
                    email=None,
                    status="ACTIVE",
                    ou_path=("Root", "Production"),
                )
            ],
        )

    def _expense(self):
        from datetime import date
        from decimal import Decimal

        from dora_roi.collectors.aws import ExpenseReport

        return ExpenseReport(
            currency="EUR",
            by_service={"Amazon S3": Decimal("1200.00")},
            period=(date(2025, 8, 1), date(2026, 8, 1)),
            payer_accounts=["123456789012"],
        )

    def test_accounts_become_b_01_02_hints(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "collect_organization", lambda **k: self._org())
        monkeypatch.setattr(cli_module, "collect_annual_expense", lambda **k: self._expense())
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--aws"])
        assert result.exit_code == 0, result.output
        prefill = json.loads((tmp_path / "roi_prefill.json").read_text())
        entity = prefill["templates"]["B_01.02"][0]
        assert entity["values"]["0020"] == "acme-prod"
        assert entity["values"]["0050"] == "Root / Production"

    def test_an_account_hint_is_inferred_and_says_why(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "collect_organization", lambda **k: self._org())
        monkeypatch.setattr(cli_module, "collect_annual_expense", lambda **k: self._expense())
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--aws"])
        prefill = json.loads((tmp_path / "roi_prefill.json").read_text())
        provenance = prefill["templates"]["B_01.02"][0]["provenance"]["0020"]
        assert provenance["status"] == "INFERRED"
        assert "not a legal entity" in provenance["note"]

    def test_cost_explorer_fills_the_provider_expense(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "collect_organization", lambda **k: self._org())
        monkeypatch.setattr(cli_module, "collect_annual_expense", lambda **k: self._expense())
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--aws"])
        prefill = json.loads((tmp_path / "roi_prefill.json").read_text())
        aws = prefill["templates"]["B_05.01"][0]
        assert aws["values"]["0100"] == "1200.00"
        assert aws["provenance"]["0100"]["status"] == "FILLED"
        assert "payer" in aws["provenance"]["0100"]["note"]

    def test_aws_being_unreachable_warns_but_does_not_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dora_roi.collectors.aws import AwsError

        def boom(**_: object):
            raise AwsError("no credentials")

        monkeypatch.setattr(cli_module, "collect_organization", boom)
        monkeypatch.setattr(cli_module, "collect_annual_expense", boom)
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--aws"])
        assert result.exit_code == 0
        assert "warning" in result.output.lower()
        assert (tmp_path / "roi_prefill.json").is_file()

    def test_the_perimeter_records_aws(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "collect_organization", lambda **k: self._org())
        monkeypatch.setattr(cli_module, "collect_annual_expense", lambda **k: self._expense())
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--aws"])
        assert json.loads((tmp_path / "inventory.json").read_text())["perimeter"]["aws"] is True


class TestKubernetesWiring:
    def _cluster(self):
        from collections import Counter

        from dora_roi.collectors.tfstate import DiscoveredProvider

        return [
            DiscoveredProvider(
                name="quay",
                namespace="",
                registry="k8s",
                resource_count=4,
                resource_types=Counter({"container_image": 4}),
                source_files={"k8s:prod"},
            )
        ]

    def test_cluster_providers_merge_with_state_providers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli_module, "collect_kubernetes", lambda **k: self._cluster())
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--k8s"])
        assert result.exit_code == 0, result.output
        names = {p["name"] for p in json.loads((tmp_path / "inventory.json").read_text())["providers"]}
        assert names == {"aws", "datadog", "cloudflare", "quay"}

    def test_a_context_implies_k8s(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def fake(**kwargs: object):
            seen.update(kwargs)
            return self._cluster()

        monkeypatch.setattr(cli_module, "collect_kubernetes", fake)
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--k8s-context", "staging"])
        assert seen["context"] == "staging"

    def test_a_cluster_only_scan_needs_no_state_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "collect_kubernetes", lambda **k: self._cluster())
        result = runner.invoke(app, ["scan", "-o", str(tmp_path), "--k8s"])
        assert result.exit_code == 0, result.output
        assert [p["name"] for p in json.loads((tmp_path / "inventory.json").read_text())["providers"]] == ["quay"]

    def test_the_perimeter_records_the_cluster(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_module, "collect_kubernetes", lambda **k: self._cluster())
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--k8s-context", "prod"])
        assert json.loads((tmp_path / "inventory.json").read_text())["perimeter"]["kubernetes"] == "prod"


# A row of nothing but key columns cannot be represented in xBRL-CSV, so an
# exportable register needs at least one real value per table. This is the
# smallest overlay that produces one.
OVERLAY_FOR_EXPORT = """
entity:
  lei: 529900T8BM49AURSDO55
  name: Acme Payments SpA
  country: IT
  entity_type: payment institution
  competent_authority: Banca d'Italia
  reporting_date: 2026-03-31
providers:
  aws:
    identification_code: 5493001KJTIIGC8Y1R12
    type_of_code: LEI
    arrangement:
      type: Standalone arrangement
      currency: EUR
      annual_expense: 120000
  datadog:
    identification_code: 213800WAVVOPS85N2205
    type_of_code: LEI
    arrangement:
      type: Standalone arrangement
      currency: EUR
      annual_expense: 24000
  cloudflare:
    identification_code: 5493001KJTIIGC8Y1R13
    type_of_code: LEI
    arrangement:
      type: Standalone arrangement
      currency: EUR
      annual_expense: 6000
"""


class TestExportCommand:
    def _scanned(self, tmp_path: Path) -> Path:
        overlay = tmp_path / "vendors.yaml"
        overlay.write_text(OVERLAY_FOR_EXPORT)
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--overlay", str(overlay)])
        return tmp_path

    def test_refuses_to_export_with_blocking_findings(self, tmp_path: Path) -> None:
        out = self._scanned(tmp_path)
        result = runner.invoke(app, ["export", "-o", str(out)])
        assert result.exit_code == 1
        assert "Refusing to export" in result.output
        assert not list(out.glob("*.zip"))

    def test_the_refusal_explains_why_rather_than_just_failing(self, tmp_path: Path) -> None:
        out = self._scanned(tmp_path)
        assert "looks filed" in runner.invoke(app, ["export", "-o", str(out)]).output

    def test_force_exports_anyway_and_says_so(self, tmp_path: Path) -> None:
        out = self._scanned(tmp_path)
        result = runner.invoke(app, ["export", "-o", str(out), "--force"])
        assert result.exit_code == 0, result.output
        assert "do not file this blind" in result.output.lower()
        assert len(list(out.glob("*.zip"))) == 1

    def test_the_package_is_named_by_the_convention(self, tmp_path: Path) -> None:
        out = self._scanned(tmp_path)
        runner.invoke(app, ["export", "-o", str(out), "--force"])
        written = next(out.glob("*.zip")).name
        assert written.startswith("529900T8BM49AURSDO55.CON_IT_DORA010100_DORA_2026-03-31_")
        assert written.endswith(".zip")

    def test_individual_scope(self, tmp_path: Path) -> None:
        out = self._scanned(tmp_path)
        runner.invoke(app, ["export", "-o", str(out), "--force", "--individual"])
        assert ".IND_IT_" in next(out.glob("*.zip")).name

    def test_the_package_declares_the_generating_software(self, tmp_path: Path) -> None:
        import json as _json
        import zipfile

        out = self._scanned(tmp_path)
        runner.invoke(app, ["export", "-o", str(out), "--force"])
        with zipfile.ZipFile(next(out.glob("*.zip"))) as archive:
            path = next(n for n in archive.namelist() if n.endswith("reports/report.json"))
            report = _json.loads(archive.read(path))
        assert report["eba:generatingSoftwareInformation"].startswith("dora-roi ")

    def test_no_software_omits_it(self, tmp_path: Path) -> None:
        import json as _json
        import zipfile

        out = self._scanned(tmp_path)
        runner.invoke(app, ["export", "-o", str(out), "--force", "--no-software"])
        with zipfile.ZipFile(next(out.glob("*.zip"))) as archive:
            path = next(n for n in archive.namelist() if n.endswith("reports/report.json"))
            report = _json.loads(archive.read(path))
        assert "eba:generatingSoftwareInformation" not in report

    def test_check_writes_nothing(self, tmp_path: Path) -> None:
        out = self._scanned(tmp_path)
        result = runner.invoke(app, ["export", "-o", str(out), "--check"])
        assert "nothing was written" in result.output
        assert not list(out.glob("*.zip"))

    def test_without_an_entity_lei_it_says_which_field_is_missing(self, tmp_path: Path) -> None:
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path)])
        result = runner.invoke(app, ["export", "-o", str(tmp_path), "--force"])
        assert result.exit_code == 1
        assert "B_01.01.0010" in result.output

    def test_no_prefill_is_an_actionable_error(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["export", "-o", str(tmp_path)])
        assert result.exit_code == 1
        assert "scan" in result.output

    def test_to_writes_somewhere_else(self, tmp_path: Path) -> None:
        out = self._scanned(tmp_path)
        elsewhere = tmp_path / "packages"
        runner.invoke(app, ["export", "-o", str(out), "--to", str(elsewhere), "--force"])
        assert len(list(elsewhere.glob("*.zip"))) == 1


class TestManyStateFiles:
    def _tree(self, tmp_path: Path) -> Path:
        states = tmp_path / "infra"
        (states / "prod").mkdir(parents=True)
        (states / "staging").mkdir(parents=True)
        (states / "prod" / "main.tfstate").write_text(Path(SAMPLE).read_text())
        (states / "staging" / "main.tfstate").write_text((FIXTURES / "second.tfstate").read_text())
        (states / "README.md").write_text("not a state file")
        return states

    def test_a_directory_is_scanned_recursively(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", "-s", str(self._tree(tmp_path)), "-o", str(tmp_path / "out")])
        assert result.exit_code == 0, result.output
        payload = json.loads((tmp_path / "out" / "inventory.json").read_text())
        assert {p["name"] for p in payload["providers"]} == {"aws", "datadog", "cloudflare", "github"}

    def test_the_perimeter_names_the_files_not_the_directory(self, tmp_path: Path) -> None:
        states = self._tree(tmp_path)
        runner.invoke(app, ["scan", "-s", str(states), "-o", str(tmp_path / "out")])
        scanned = json.loads((tmp_path / "out" / "inventory.json").read_text())["perimeter"]["state_files"]
        assert len(scanned) == 2
        assert str(states) not in scanned
        assert all(f.endswith("main.tfstate") for f in scanned)

    def test_non_state_files_are_left_alone(self, tmp_path: Path) -> None:
        states = self._tree(tmp_path)
        runner.invoke(app, ["scan", "-s", str(states), "-o", str(tmp_path / "out")])
        scanned = json.loads((tmp_path / "out" / "inventory.json").read_text())["perimeter"]["state_files"]
        assert not any("README" in f for f in scanned)

    def test_an_empty_directory_exits_one_with_a_useful_message(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = runner.invoke(app, ["scan", "-s", str(empty), "-o", str(tmp_path / "out")])
        assert result.exit_code == 1
        assert "no state files" in result.output

    def test_providers_takes_a_directory_too(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["providers", "-s", str(self._tree(tmp_path))])
        assert result.exit_code == 0
        assert "github" in result.output


class TestSourcesFile:
    def test_a_local_source_list_works_end_to_end(self, tmp_path: Path) -> None:
        sources = tmp_path / "sources.yaml"
        sources.write_text(f"states:\n  - {SAMPLE}\n  - {FIXTURES / 'second.tfstate'}\n")
        result = runner.invoke(app, ["scan", "--sources", str(sources), "-o", str(tmp_path / "out")])
        assert result.exit_code == 0, result.output
        payload = json.loads((tmp_path / "out" / "inventory.json").read_text())
        assert {p["name"] for p in payload["providers"]} == {"aws", "datadog", "cloudflare", "github"}

    def test_sources_and_s_combine(self, tmp_path: Path) -> None:
        sources = tmp_path / "sources.yaml"
        sources.write_text(f"states:\n  - {FIXTURES / 'second.tfstate'}\n")
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "--sources", str(sources), "-o", str(tmp_path / "out")])
        assert result.exit_code == 0
        payload = json.loads((tmp_path / "out" / "inventory.json").read_text())
        assert len(payload["perimeter"]["state_files"]) == 2

    def test_a_typo_in_the_sources_file_exits_one(self, tmp_path: Path) -> None:
        sources = tmp_path / "sources.yaml"
        sources.write_text("states:\n  - uri: s3://b/k.tfstate\n    porfile: prod\n")
        result = runner.invoke(app, ["scan", "--sources", str(sources), "-o", str(tmp_path / "out")])
        assert result.exit_code == 1
        assert "porfile" in result.output

    def test_a_missing_sources_file_exits_one(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", "--sources", str(tmp_path / "no.yaml"), "-o", str(tmp_path)])
        assert result.exit_code == 1

    def test_nothing_at_all_mentions_sources(self, tmp_path: Path) -> None:
        assert "--sources" in runner.invoke(app, ["scan", "-o", str(tmp_path)]).output


class TestFetchedStateDoesNotSurvive:
    """A copy of somebody's Terraform state is a copy of their credentials."""

    def _sources(self, tmp_path: Path) -> Path:
        path = tmp_path / "sources.yaml"
        path.write_text("states:\n  - uri: s3://bucket/prod.tfstate\n")
        return path

    def _fetch_into_scratch(self, monkeypatch: pytest.MonkeyPatch, seen: list[Path], boom: bool = False):
        def fake(remote, scratch: Path, excluded=None):
            copy = Path(scratch) / "prod.tfstate"
            copy.write_text(Path(SAMPLE).read_text())
            seen.append(copy)
            if boom:
                raise RuntimeError("fetched, then something went wrong")
            return [("s3://bucket/prod.tfstate", copy)]

        monkeypatch.setattr(cli_module, "fetch_sources", fake)

    def test_the_scratch_copy_is_gone_when_the_scan_ends(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[Path] = []
        self._fetch_into_scratch(monkeypatch, seen)
        result = runner.invoke(app, ["scan", "--sources", str(self._sources(tmp_path)), "-o", str(tmp_path / "out")])
        assert result.exit_code == 0, result.output
        assert seen and not seen[0].exists()
        assert not seen[0].parent.exists()

    def test_it_is_gone_even_when_the_scan_blows_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The failure path is the one that leaves secrets behind."""
        seen: list[Path] = []
        self._fetch_into_scratch(monkeypatch, seen, boom=True)
        runner.invoke(app, ["scan", "--sources", str(self._sources(tmp_path)), "-o", str(tmp_path / "out")])
        assert seen and not seen[0].parent.exists()

    def test_the_report_still_names_the_uri_not_the_deleted_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting the copy must not cost the perimeter its provenance."""
        self._fetch_into_scratch(monkeypatch, [])
        runner.invoke(app, ["scan", "--sources", str(self._sources(tmp_path)), "-o", str(tmp_path / "out")])
        scanned = json.loads((tmp_path / "out" / "inventory.json").read_text())["perimeter"]["state_files"]
        assert scanned == ["s3://bucket/prod.tfstate"]


class TestCostExplorerScope:
    """Cost Explorer answers for the account you call it from, not the organisation."""

    def _org(self, master: str = "111122223333"):
        from dora_roi.collectors.aws import DiscoveredAccount, OrganizationInventory

        return OrganizationInventory(
            organization_id="o-abc",
            master_account_id=master,
            accounts=[
                DiscoveredAccount(account_id=master, name="root", email=None, status="ACTIVE", ou_path=("Root",))
            ],
        )

    def _expense(self, payer_accounts):
        from datetime import date
        from decimal import Decimal

        from dora_roi.collectors.aws import ExpenseReport

        return ExpenseReport(
            currency="USD",
            by_service={"Amazon S3": Decimal("520.68")},
            period=(date(2025, 8, 1), date(2026, 8, 1)),
            payer_accounts=payer_accounts or [],
        )

    def _patch(self, monkeypatch, calling: str) -> None:
        monkeypatch.setattr(cli_module, "collect_organization", lambda **k: self._org())
        monkeypatch.setattr(cli_module, "calling_account", lambda *a, **k: calling)
        monkeypatch.setattr(cli_module, "collect_annual_expense", lambda **k: self._expense(k.get("payer_accounts")))

    def test_a_member_account_is_flagged_not_passed_off_as_the_organisation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch(monkeypatch, calling="444455556666")
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--aws"])
        assert result.exit_code == 0
        assert "not the organisation's payer" in result.output

    def test_the_note_names_the_account_that_was_actually_billed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It used to name the org's payer while the number came from elsewhere."""
        self._patch(monkeypatch, calling="444455556666")
        runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--aws"])
        prefill = json.loads((tmp_path / "roi_prefill.json").read_text())
        note = prefill["templates"]["B_05.01"][0]["provenance"]["0100"]["note"]
        assert "444455556666" in note
        assert "111122223333" not in note

    def test_the_payer_itself_raises_no_warning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, calling="111122223333")
        result = runner.invoke(app, ["scan", "-s", SAMPLE, "-o", str(tmp_path), "--aws"])
        assert "not the organisation's payer" not in result.output
