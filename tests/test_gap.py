"""Gap report."""

from __future__ import annotations

import json

from dora_roi.models.enums import FieldStatus, Priority
from dora_roi.models.templates import (
    FIELD_CATALOG,
    BusinessFunction,
    ContractualArrangementGeneral,
    EntityMaintainingRegister,
    RegisterOfInformation,
    SupplyChainLink,
    ThirdPartyProvider,
)
from dora_roi.report.gap import (
    MODEL_FIELD_MAP,
    GapEntry,
    build_gap_report,
    summarize,
    to_json,
    to_markdown,
)

TOTAL_CATALOG_FIELDS = sum(len(fields) for fields in FIELD_CATALOG.values())
B_05_01_MANDATORY = 6  # 0010, 0020, 0050, 0070, 0080, 0110


def register_with_one_bare_provider() -> RegisterOfInformation:
    return RegisterOfInformation(providers=[ThirdPartyProvider()])


class TestModelFieldMap:
    def test_covers_every_template_now_that_all_fifteen_are_modelled(self) -> None:
        assert set(MODEL_FIELD_MAP) == set(FIELD_CATALOG)

    def test_maps_every_catalog_code_to_a_python_field(self) -> None:
        for template, by_code in MODEL_FIELD_MAP.items():
            assert set(by_code) == {f.code for f in FIELD_CATALOG[template]}


class TestCoverage:
    def test_nothing_is_invisible(self) -> None:
        entries = build_gap_report(RegisterOfInformation())
        assert len(entries) == TOTAL_CATALOG_FIELDS

    def test_all_fifteen_templates_appear_in_the_summary(self) -> None:
        summary = summarize(build_gap_report(RegisterOfInformation()))
        assert set(summary.by_template) == set(FIELD_CATALOG)
        assert len(summary.by_template) == 15

    def test_a_template_with_no_rows_still_reports_every_field(self) -> None:
        entries = build_gap_report(RegisterOfInformation())
        b0601 = [e for e in entries if e.template == "B_06.01"]
        assert len(b0601) == 10
        assert all(e.status is FieldStatus.MISSING for e in b0601)
        assert all(e.row_key is None for e in b0601)

    def test_the_late_templates_are_row_aware(self) -> None:
        """A row in any of the eleven late templates is read, not synthesised."""
        roi = RegisterOfInformation(functions=[BusinessFunction(name="Payment execution")])
        roi.functions[0].mark("name", FieldStatus.FILLED, source="overlay")
        entry = next(e for e in build_gap_report(roi) if e.template == "B_06.01" and e.code == "0030")
        assert entry.status is FieldStatus.FILLED
        assert entry.row_key == "Payment execution"

    def test_a_modelled_template_with_no_rows_still_reports(self) -> None:
        entries = build_gap_report(RegisterOfInformation())
        assert [e for e in entries if e.template == "B_05.01"]


class TestBareProviderRow:
    def test_blocking_missing_count(self) -> None:
        entries = build_gap_report(register_with_one_bare_provider())
        blocking_missing = [
            e
            for e in entries
            if e.template == "B_05.01" and e.priority is Priority.BLOCKING and e.status is FieldStatus.MISSING
        ]
        assert len(blocking_missing) == B_05_01_MANDATORY

    def test_summary_counts_agree(self) -> None:
        summary = summarize(build_gap_report(register_with_one_bare_provider()))
        assert summary.by_template["B_05.01"].blocking_missing == B_05_01_MANDATORY
        assert summary.by_template["B_05.01"].total == 12

    def test_a_bare_row_produces_one_entry_per_field(self) -> None:
        entries = build_gap_report(register_with_one_bare_provider())
        assert len([e for e in entries if e.template == "B_05.01"]) == 12


class TestStatusReflectsProvenance:
    def test_a_marked_field_reports_its_status_and_source(self) -> None:
        row = ThirdPartyProvider(legal_name="Datadog, Inc.")
        row.mark("legal_name", FieldStatus.INFERRED, source="mapping", note="vendor hint")
        entries = build_gap_report(RegisterOfInformation(providers=[row]))
        entry = next(e for e in entries if e.template == "B_05.01" and e.code == "0050")
        assert entry.status is FieldStatus.INFERRED
        assert entry.source == "mapping"
        assert entry.note == "vendor hint"

    def test_filling_a_field_removes_it_from_the_blocking_gaps(self) -> None:
        row = ThirdPartyProvider(legal_name="Datadog, Inc.")
        row.mark("legal_name", FieldStatus.FILLED, source="gleif:exact")
        summary = summarize(build_gap_report(RegisterOfInformation(providers=[row])))
        assert summary.by_template["B_05.01"].blocking_missing == B_05_01_MANDATORY - 1


class TestUnprovenancedValues:
    """A value with no provenance is a pipeline bug, not a silent MISSING."""

    def test_it_is_still_missing_because_provenance_is_the_truth(self) -> None:
        row = ThirdPartyProvider(legal_name="Set but never marked")
        entry = next(
            e
            for e in build_gap_report(RegisterOfInformation(providers=[row]))
            if e.template == "B_05.01" and e.code == "0050"
        )
        assert entry.status is FieldStatus.MISSING

    def test_but_it_says_so_loudly(self) -> None:
        row = ThirdPartyProvider(legal_name="Set but never marked")
        entry = next(
            e
            for e in build_gap_report(RegisterOfInformation(providers=[row]))
            if e.template == "B_05.01" and e.code == "0050"
        )
        assert entry.unprovenanced is True
        assert "provenance" in (entry.note or "")

    def test_the_summary_counts_them(self) -> None:
        row = ThirdPartyProvider(legal_name="x", headquarters_country="IT")
        summary = summarize(build_gap_report(RegisterOfInformation(providers=[row])))
        assert summary.unprovenanced == 2

    def test_a_properly_marked_row_reports_none(self) -> None:
        row = ThirdPartyProvider(legal_name="x").mark("legal_name", FieldStatus.INFERRED, source="mapping")
        summary = summarize(build_gap_report(RegisterOfInformation(providers=[row])))
        assert summary.unprovenanced == 0


class TestConditionalMandatory:
    def test_a_conditional_field_is_blocking_and_says_when(self) -> None:
        entry = next(
            e for e in build_gap_report(RegisterOfInformation()) if e.template == "B_02.02" and e.code == "0150"
        )
        assert entry.priority is Priority.BLOCKING
        assert "only when" in (entry.note or "")


class TestOrdering:
    def test_blocking_and_missing_come_first(self) -> None:
        entries = build_gap_report(register_with_one_bare_provider())
        first = entries[0]
        assert first.priority is Priority.BLOCKING
        assert first.status is FieldStatus.MISSING

    def test_every_blocking_missing_precedes_every_other_entry(self) -> None:
        entries = build_gap_report(register_with_one_bare_provider())
        ranks = [0 if (e.priority is Priority.BLOCKING and e.status is FieldStatus.MISSING) else 1 for e in entries]
        assert ranks == sorted(ranks)

    def test_within_a_group_ordering_is_by_template_then_code(self) -> None:
        entries = build_gap_report(RegisterOfInformation())
        blocking = [(e.template, e.code) for e in entries if e.priority is Priority.BLOCKING]
        assert blocking == sorted(blocking)

    def test_ordering_is_stable_across_runs(self) -> None:
        a = build_gap_report(register_with_one_bare_provider())
        b = build_gap_report(register_with_one_bare_provider())
        assert [(e.template, e.code, e.row_key) for e in a] == [(e.template, e.code, e.row_key) for e in b]


class TestSummary:
    def test_totals_add_up(self) -> None:
        summary = summarize(build_gap_report(RegisterOfInformation()))
        counted = sum(t.total for t in summary.by_template.values())
        assert counted == summary.total == TOTAL_CATALOG_FIELDS

    def test_status_buckets_partition_the_entries(self) -> None:
        row = ThirdPartyProvider(legal_name="x").mark("legal_name", FieldStatus.FILLED, source="overlay")
        summary = summarize(build_gap_report(RegisterOfInformation(providers=[row])))
        assert summary.filled + summary.inferred + summary.missing == summary.total

    def test_an_empty_report_summarises_to_zero(self) -> None:
        summary = summarize([])
        assert summary.total == 0
        assert summary.by_template == {}


class TestMarkdown:
    def test_leads_with_the_blocking_gaps(self) -> None:
        md = to_markdown(build_gap_report(register_with_one_bare_provider()))
        assert "Blocking gaps first" in md

    def test_has_a_summary_table_with_every_template(self) -> None:
        md = to_markdown(build_gap_report(RegisterOfInformation()))
        for template in FIELD_CATALOG:
            assert template in md

    def test_carries_the_no_compliance_disclaimer(self) -> None:
        md = to_markdown(build_gap_report(RegisterOfInformation()))
        assert "does not make" in md.lower() or "not a compliance" in md.lower()

    def test_names_the_field_not_only_the_code(self) -> None:
        md = to_markdown(build_gap_report(register_with_one_bare_provider()))
        assert "Legal name of the ICT third-party service provider" in md

    def test_flags_unverified_field_names(self) -> None:
        md = to_markdown(build_gap_report(RegisterOfInformation()))
        assert "B_03.02" in md


class TestJson:
    def test_is_valid_json_with_entries_and_summary(self) -> None:
        payload = json.loads(to_json(build_gap_report(register_with_one_bare_provider())))
        assert len(payload["entries"]) == TOTAL_CATALOG_FIELDS
        assert payload["summary"]["by_template"]["B_05.01"]["blocking_missing"] == B_05_01_MANDATORY

    def test_entries_serialise_their_enums_as_strings(self) -> None:
        payload = json.loads(to_json(build_gap_report(RegisterOfInformation())))
        entry = payload["entries"][0]
        assert entry["status"] == "MISSING"
        assert entry["priority"] == "BLOCKING"

    def test_is_stable_enough_for_a_ci_diff(self) -> None:
        a = to_json(build_gap_report(register_with_one_bare_provider()))
        b = to_json(build_gap_report(register_with_one_bare_provider()))
        assert a == b


class TestSeveralRows:
    def test_each_row_gets_its_own_entries_keyed_by_row(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(legal_name="A"), ThirdPartyProvider(legal_name="B")])
        entries = [e for e in build_gap_report(roi) if e.template == "B_05.01"]
        assert len(entries) == 24
        assert {e.row_key for e in entries} == {"A", "B"}

    def test_row_key_falls_back_when_the_key_field_is_empty(self) -> None:
        roi = RegisterOfInformation(providers=[ThirdPartyProvider(), ThirdPartyProvider()])
        keys = {e.row_key for e in build_gap_report(roi) if e.template == "B_05.01"}
        assert keys == {"#1", "#2"}

    def test_the_whole_register_is_walked(self) -> None:
        roi = RegisterOfInformation(
            entity=EntityMaintainingRegister(name="Acme"),
            arrangements=[ContractualArrangementGeneral(arrangement_reference="ARR-AWS-001")],
            providers=[ThirdPartyProvider(legal_name="AWS")],
            supply_chain=[SupplyChainLink(arrangement_reference="ARR-AWS-001", rank=1)],
        )
        templates = {e.template for e in build_gap_report(roi)}
        assert templates == set(FIELD_CATALOG)


def test_gap_entry_is_hashable_and_comparable() -> None:
    entry = GapEntry(
        template="B_05.01",
        code="0010",
        name="x",
        row_key=None,
        status=FieldStatus.MISSING,
        source=None,
        priority=Priority.BLOCKING,
        note=None,
    )
    assert entry in {entry}
