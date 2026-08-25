"""Terraform state collector."""

from collections import Counter
from pathlib import Path

import pytest

from dora_roi.collectors.tfstate import (
    DiscoveredProvider,
    TfstateError,
    merge_providers,
    parse_many,
    parse_state_file,
    resolve_state_paths,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample.tfstate"
SECOND = FIXTURES / "second.tfstate"


@pytest.fixture
def discovered() -> dict[str, DiscoveredProvider]:
    return {p.name: p for p in parse_state_file(SAMPLE)}


class TestProviderDiscovery:
    def test_finds_every_distinct_provider(self, discovered: dict[str, DiscoveredProvider]) -> None:
        assert set(discovered) == {"aws", "datadog", "cloudflare"}

    def test_keeps_registry_and_namespace(self, discovered: dict[str, DiscoveredProvider]) -> None:
        assert discovered["aws"].registry == "registry.terraform.io"
        assert discovered["aws"].namespace == "hashicorp"
        # Namespace case is preserved as written in state: DataDog, not datadog.
        assert discovered["datadog"].namespace == "DataDog"

    def test_results_are_sorted_by_resource_count_desc(self) -> None:
        providers = parse_state_file(SAMPLE)
        assert [p.name for p in providers] == ["aws", "datadog", "cloudflare"]

    def test_records_the_source_file(self, discovered: dict[str, DiscoveredProvider]) -> None:
        assert discovered["aws"].source_files == {str(SAMPLE)}


class TestInstanceCounting:
    def test_counts_instances_not_resource_blocks(self, discovered: dict[str, DiscoveredProvider]) -> None:
        # 1 bucket + 3 instances (count) + 1 db = 5, across three resource blocks.
        assert discovered["aws"].resource_count == 5
        assert discovered["datadog"].resource_count == 2
        assert discovered["cloudflare"].resource_count == 1

    def test_resource_types_are_counted_per_type(self, discovered: dict[str, DiscoveredProvider]) -> None:
        assert discovered["aws"].resource_types == Counter(
            {"aws_instance": 3, "aws_s3_bucket": 1, "aws_db_instance": 1}
        )


class TestDataSourcesAreNotVendorRelationships:
    def test_data_sources_are_ignored(self, discovered: dict[str, DiscoveredProvider]) -> None:
        assert "aws_caller_identity" not in discovered["aws"].resource_types

    def test_a_state_of_only_data_sources_discovers_nothing(self, tmp_path: Path) -> None:
        state = tmp_path / "data-only.tfstate"
        state.write_text(
            '{"version": 4, "resources": [{"mode": "data", "type": "aws_ami", "name": "x",'
            ' "provider": "provider[\\"registry.terraform.io/hashicorp/aws\\"]",'
            ' "instances": [{"attributes": {}}]}]}'
        )
        assert parse_state_file(state) == []


class TestRegionExtraction:
    def test_region_attribute_is_used(self, discovered: dict[str, DiscoveredProvider]) -> None:
        assert "eu-south-1" in discovered["aws"].regions

    def test_region_falls_back_to_the_arn(self, discovered: dict[str, DiscoveredProvider]) -> None:
        # The RDS instance has no region attribute; eu-west-1 comes from its ARN.
        assert "eu-west-1" in discovered["aws"].regions

    def test_global_arns_contribute_no_region(self, discovered: dict[str, DiscoveredProvider]) -> None:
        # arn:aws:s3:::acme-logs has an empty region segment: it must not appear.
        assert "" not in discovered["aws"].regions
        assert discovered["aws"].regions == {"eu-south-1", "eu-west-1"}

    def test_providers_without_regions_get_an_empty_set(self, discovered: dict[str, DiscoveredProvider]) -> None:
        assert discovered["datadog"].regions == set()

    def test_region_is_never_guessed(self, tmp_path: Path) -> None:
        state = tmp_path / "no-region.tfstate"
        state.write_text(
            '{"version": 4, "resources": [{"mode": "managed", "type": "aws_iam_role", "name": "r",'
            ' "provider": "provider[\\"registry.terraform.io/hashicorp/aws\\"]",'
            ' "instances": [{"attributes": {"name": "r", "arn": "arn:aws:iam::1234:role/r"}}]}]}'
        )
        assert parse_state_file(state)[0].regions == set()


class TestProviderAliases:
    def test_an_aliased_provider_is_the_same_provider(self, discovered: dict[str, DiscoveredProvider]) -> None:
        # The RDS instance uses `...aws"].eu` inside a module; still one `aws`.
        assert discovered["aws"].resource_types["aws_db_instance"] == 1
        assert len(discovered) == 3


class TestParseMany:
    def test_merges_counts_across_files(self) -> None:
        merged = {p.name: p for p in parse_many([SAMPLE, SECOND])}
        assert merged["aws"].resource_count == 6
        assert merged["aws"].resource_types["aws_s3_bucket"] == 2

    def test_unions_regions_and_source_files(self) -> None:
        merged = {p.name: p for p in parse_many([SAMPLE, SECOND])}
        assert merged["aws"].regions == {"eu-south-1", "eu-west-1", "eu-central-1"}
        assert merged["aws"].source_files == {str(SAMPLE), str(SECOND)}

    def test_providers_unique_to_one_file_survive(self) -> None:
        merged = {p.name: p for p in parse_many([SAMPLE, SECOND])}
        assert set(merged) == {"aws", "datadog", "cloudflare", "github"}

    def test_merged_result_is_sorted_by_count(self) -> None:
        assert [p.name for p in parse_many([SAMPLE, SECOND])][0] == "aws"

    def test_no_files_is_not_an_error(self) -> None:
        assert parse_many([]) == []

    def test_merge_does_not_mutate_its_inputs(self) -> None:
        first = parse_state_file(SAMPLE)
        before = first[0].resource_count
        merge_providers([first, parse_state_file(SECOND)])
        assert first[0].resource_count == before


class TestErrors:
    def test_missing_file_names_the_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.tfstate"
        with pytest.raises(TfstateError, match="nope.tfstate"):
            parse_state_file(missing)

    def test_wrong_version_says_which_version_it_found(self) -> None:
        with pytest.raises(TfstateError, match="version 3"):
            parse_state_file(FIXTURES / "v3.tfstate")

    def test_wrong_version_says_what_to_do(self) -> None:
        with pytest.raises(TfstateError, match="terraform state pull|tofu"):
            parse_state_file(FIXTURES / "v3.tfstate")

    def test_malformed_json_raises_tfstate_error(self) -> None:
        with pytest.raises(TfstateError, match="not valid JSON"):
            parse_state_file(FIXTURES / "malformed.tfstate")

    def test_errors_are_chained(self) -> None:
        with pytest.raises(TfstateError) as exc:
            parse_state_file(FIXTURES / "malformed.tfstate")
        assert exc.value.__cause__ is not None

    def test_a_json_document_that_is_not_a_state_file(self, tmp_path: Path) -> None:
        state = tmp_path / "list.tfstate"
        state.write_text("[1, 2, 3]")
        with pytest.raises(TfstateError, match="object"):
            parse_state_file(state)

    def test_missing_version_key(self, tmp_path: Path) -> None:
        state = tmp_path / "noversion.tfstate"
        state.write_text('{"resources": []}')
        with pytest.raises(TfstateError, match="version"):
            parse_state_file(state)

    def test_unparseable_provider_string_is_reported(self, tmp_path: Path) -> None:
        state = tmp_path / "weird.tfstate"
        state.write_text(
            '{"version": 4, "resources": [{"mode": "managed", "type": "x", "name": "y",'
            ' "provider": "something-else", "instances": [{}]}]}'
        )
        with pytest.raises(TfstateError, match="provider"):
            parse_state_file(state)

    def test_parse_many_reports_which_file_failed(self, tmp_path: Path) -> None:
        with pytest.raises(TfstateError, match="v3.tfstate"):
            parse_many([SAMPLE, FIXTURES / "v3.tfstate"])


class TestEmptyState:
    def test_a_state_with_no_resources_is_valid_and_empty(self) -> None:
        assert parse_state_file(FIXTURES / "empty.tfstate") == []


class TestResolveStatePaths:
    """What a person types, expanded into the files they meant."""

    def test_a_file_is_itself(self) -> None:
        assert resolve_state_paths([SAMPLE]) == [SAMPLE]

    def test_a_directory_is_every_state_file_under_it(self, tmp_path: Path) -> None:
        (tmp_path / "prod").mkdir()
        (tmp_path / "stg" / "deep").mkdir(parents=True)
        for target in ("prod/a.tfstate", "stg/deep/b.tfstate"):
            (tmp_path / target).write_text(SAMPLE.read_text())
        assert [p.name for p in resolve_state_paths([tmp_path])] == ["a.tfstate", "b.tfstate"]

    def test_a_directory_ignores_files_that_are_not_state(self, tmp_path: Path) -> None:
        (tmp_path / "a.tfstate").write_text(SAMPLE.read_text())
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "main.tf").write_text("")
        assert [p.name for p in resolve_state_paths([tmp_path])] == ["a.tfstate"]

    def test_an_empty_directory_says_what_it_looked_for(self, tmp_path: Path) -> None:
        with pytest.raises(TfstateError, match=r"\.tfstate"):
            resolve_state_paths([tmp_path])

    def test_a_glob_the_shell_did_not_expand(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "a.tfstate").write_text(SAMPLE.read_text())
        (tmp_path / "b.tfstate").write_text(SAMPLE.read_text())
        monkeypatch.chdir(tmp_path)
        assert [p.name for p in resolve_state_paths(["*.tfstate"])] == ["a.tfstate", "b.tfstate"]

    def test_a_glob_matching_nothing_says_so(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(TfstateError, match="matched no files"):
            resolve_state_paths(["*.tfstate"])

    def test_the_same_file_twice_is_read_once(self, tmp_path: Path) -> None:
        (tmp_path / "a.tfstate").write_text(SAMPLE.read_text())
        resolved = resolve_state_paths([tmp_path, tmp_path / "a.tfstate"])
        assert len(resolved) == 1

    def test_ordering_is_stable(self, tmp_path: Path) -> None:
        for name in ("c.tfstate", "a.tfstate", "b.tfstate"):
            (tmp_path / name).write_text(SAMPLE.read_text())
        assert resolve_state_paths([tmp_path]) == resolve_state_paths([tmp_path])

    def test_a_missing_file_is_left_for_the_parser_to_explain(self, tmp_path: Path) -> None:
        """The parser's message already says how to export a state file."""
        missing = tmp_path / "nope.tfstate"
        assert resolve_state_paths([missing]) == [missing]
        with pytest.raises(TfstateError, match="terraform state pull"):
            parse_many(resolve_state_paths([missing]))


class TestUtilityProvidersAreNotVendors:
    """`random`, `time` and `archive` contract with nobody."""

    def state_with(self, tmp_path: Path, *providers: str) -> Path:
        resources = ", ".join(
            f'{{"mode": "managed", "type": "{p}_thing", "name": "x",'
            f' "provider": "provider[\\"registry.terraform.io/hashicorp/{p}\\"]",'
            f' "instances": [{{"attributes": {{}}}}]}}'
            for p in providers
        )
        path = tmp_path / "s.tfstate"
        path.write_text(f'{{"version": 4, "resources": [{resources}]}}')
        return path

    def test_they_are_not_reported(self, tmp_path: Path) -> None:
        found = parse_state_file(self.state_with(tmp_path, "aws", "random", "time", "archive", "null"))
        assert [p.name for p in found] == ["aws"]

    def test_a_state_of_only_utilities_discovers_nothing(self, tmp_path: Path) -> None:
        assert parse_state_file(self.state_with(tmp_path, "random", "local", "template")) == []

    def test_a_real_vendor_beside_them_survives(self, tmp_path: Path) -> None:
        found = parse_state_file(self.state_with(tmp_path, "random", "datadog"))
        assert [p.name for p in found] == ["datadog"]


class TestOriginIsWhatGetsCited:
    def test_the_path_is_cited_by_default(self) -> None:
        assert parse_state_file(SAMPLE)[0].source_files == {str(SAMPLE)}

    def test_an_origin_overrides_it(self) -> None:
        found = parse_state_file(SAMPLE, origin="s3://bucket/env:/production/be/terraform.tfstate")
        assert found[0].source_files == {"s3://bucket/env:/production/be/terraform.tfstate"}

    def test_parse_many_threads_origins_through(self) -> None:
        origins = {SAMPLE: "s3://b/one.tfstate", SECOND: "s3://b/two.tfstate"}
        merged = {p.name: p for p in parse_many([SAMPLE, SECOND], origins)}
        assert merged["aws"].source_files == {"s3://b/one.tfstate", "s3://b/two.tfstate"}
        assert not any("/" in f and f.startswith("/") for f in merged["aws"].source_files)
