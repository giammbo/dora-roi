"""State that lives in S3, across accounts (moto-backed: no real AWS, ever)."""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from dora_roi.collectors.sources import SourceError, StateSource, fetch_sources, load_aws_sweep, load_sources
from dora_roi.collectors.tfstate import TfstateError, parse_many

FIXTURES = Path(__file__).parent / "fixtures"
STATE = (FIXTURES / "sample.tfstate").read_text()
SECOND = (FIXTURES / "second.tfstate").read_text()
REGION = "eu-south-1"


@pytest.fixture
def buckets():
    """Two buckets, as if they were two accounts."""
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        for name in ("acme-tfstate-prod", "acme-tfstate-stg"):
            client.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
        client.put_object(Bucket="acme-tfstate-prod", Key="network/terraform.tfstate", Body=STATE.encode())
        client.put_object(Bucket="acme-tfstate-prod", Key="data/terraform.tfstate", Body=SECOND.encode())
        client.put_object(Bucket="acme-tfstate-prod", Key="notes/README.md", Body=b"not state")
        client.put_object(Bucket="acme-tfstate-stg", Key="main.tfstate", Body=SECOND.encode())
        yield client


def factory(_source: StateSource):
    """Every source gets the same mocked client; the point is the plumbing, not the creds."""
    return boto3.client("s3", region_name=REGION)


class TestLoadSources:
    def test_a_bare_uri_is_a_source(self, tmp_path: Path) -> None:
        path = tmp_path / "s.yaml"
        path.write_text("states:\n  - s3://b/k.tfstate\n")
        assert load_sources(path) == [StateSource(uri="s3://b/k.tfstate")]

    def test_every_field_survives_the_loader(self, tmp_path: Path) -> None:
        """Each field, asserted individually.

        `workspace` was declared on the model and accepted by the key check, and
        the loader never passed it on — so every source came back with
        workspace=None and the scan refused to run. The workspace tests below all
        build a StateSource directly, which is exactly why none of them saw it.
        A field is not wired until it has come through the YAML.
        """
        path = tmp_path / "s.yaml"
        path.write_text(
            "states:\n"
            "  - uri: s3://b/k/\n"
            "    profile: prod\n"
            "    role_arn: arn:aws:iam::1:role/ro\n"
            "    workspace: production\n"
        )
        loaded = load_sources(path)[0]
        assert loaded.uri == "s3://b/k/"
        assert loaded.profile == "prod"
        assert loaded.role_arn == "arn:aws:iam::1:role/ro"
        assert loaded.workspace == "production"

    def test_no_field_is_silently_dropped(self, tmp_path: Path) -> None:
        """Guards the whole shape at once, so a new field cannot be forgotten."""
        import dataclasses

        path = tmp_path / "s.yaml"
        path.write_text("states:\n  - uri: s3://b/k/\n    profile: p\n    role_arn: a\n    workspace: w\n")
        loaded = load_sources(path)[0]
        unset = [f.name for f in dataclasses.fields(loaded) if getattr(loaded, f.name) is None]
        assert unset == [], f"the loader dropped: {unset}"

    def test_credentials_are_per_source(self, tmp_path: Path) -> None:
        path = tmp_path / "s.yaml"
        path.write_text(
            "states:\n"
            "  - uri: s3://prod/a.tfstate\n    profile: prod\n"
            "  - uri: s3://stg/b.tfstate\n    role_arn: arn:aws:iam::2:role/ro\n"
        )
        loaded = load_sources(path)
        assert loaded[0].profile == "prod"
        assert loaded[1].role_arn == "arn:aws:iam::2:role/ro"

    def test_local_paths_are_sources_too(self, tmp_path: Path) -> None:
        path = tmp_path / "s.yaml"
        path.write_text("states:\n  - ./infra\n")
        assert load_sources(path)[0].is_s3 is False

    def test_a_typo_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "s.yaml"
        path.write_text("states:\n  - uri: s3://b/k\n    porfile: prod\n")
        with pytest.raises(SourceError, match="porfile"):
            load_sources(path)

    def test_a_missing_uri_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "s.yaml"
        path.write_text("states:\n  - profile: prod\n")
        with pytest.raises(SourceError, match="uri"):
            load_sources(path)

    def test_the_wrong_top_level_shape_says_what_is_expected(self, tmp_path: Path) -> None:
        path = tmp_path / "s.yaml"
        path.write_text("- s3://b/k\n")
        with pytest.raises(SourceError, match="states"):
            load_sources(path)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="nope.yaml"):
            load_sources(tmp_path / "nope.yaml")

    def test_an_empty_file_is_no_sources(self, tmp_path: Path) -> None:
        path = tmp_path / "s.yaml"
        path.write_text("")
        assert load_sources(path) == []


class TestFetchFromS3:
    def test_one_object(self, buckets, tmp_path: Path) -> None:
        source = StateSource(uri="s3://acme-tfstate-prod/network/terraform.tfstate")
        fetched = fetch_sources([source], tmp_path, client_factory=factory)
        assert len(fetched) == 1
        uri, path = fetched[0]
        assert uri == "s3://acme-tfstate-prod/network/terraform.tfstate"
        assert path.read_text() == STATE

    def test_a_prefix_fetches_every_state_file_under_it(self, buckets, tmp_path: Path) -> None:
        fetched = fetch_sources([StateSource(uri="s3://acme-tfstate-prod/")], tmp_path, client_factory=factory)
        assert {uri for uri, _ in fetched} == {
            "s3://acme-tfstate-prod/data/terraform.tfstate",
            "s3://acme-tfstate-prod/network/terraform.tfstate",
        }

    def test_a_prefix_ignores_what_is_not_state(self, buckets, tmp_path: Path) -> None:
        fetched = fetch_sources([StateSource(uri="s3://acme-tfstate-prod/")], tmp_path, client_factory=factory)
        assert not any("README" in uri for uri, _ in fetched)

    def test_several_buckets_in_one_run(self, buckets, tmp_path: Path) -> None:
        sources = [
            StateSource(uri="s3://acme-tfstate-prod/network/terraform.tfstate", profile="prod"),
            StateSource(uri="s3://acme-tfstate-stg/main.tfstate", profile="staging"),
        ]
        fetched = fetch_sources(sources, tmp_path, client_factory=factory)
        assert len(fetched) == 2
        providers = {p.name for p in parse_many([path for _, path in fetched])}
        assert providers == {"aws", "datadog", "cloudflare", "github"}

    def test_files_from_different_sources_do_not_collide(self, buckets, tmp_path: Path) -> None:
        sources = [
            StateSource(uri="s3://acme-tfstate-prod/network/terraform.tfstate"),
            StateSource(uri="s3://acme-tfstate-prod/data/terraform.tfstate"),
        ]
        fetched = fetch_sources(sources, tmp_path, client_factory=factory)
        paths = [path for _, path in fetched]
        assert len(set(paths)) == 2
        assert paths[0].read_text() != paths[1].read_text()

    def test_a_shared_basename_within_one_prefix_does_not_collide(self, buckets, tmp_path: Path) -> None:
        """`prod/terraform.tfstate` and `data/terraform.tfstate` are two stacks, not one.

        The commonest S3 layout there is. Keying the local copy on the basename
        made the second overwrite the first: one stack counted twice, the other
        lost, and nothing in the output to show it had happened.
        """
        fetched = fetch_sources([StateSource(uri="s3://acme-tfstate-prod/")], tmp_path, client_factory=factory)
        paths = [path for _, path in fetched]
        assert len(set(paths)) == len(paths) == 2
        assert {p.read_text() for p in paths} == {STATE, SECOND}

    def test_the_local_copy_mirrors_the_key(self, buckets, tmp_path: Path) -> None:
        fetched = fetch_sources([StateSource(uri="s3://acme-tfstate-prod/")], tmp_path, client_factory=factory)
        assert any(str(path).endswith("network/terraform.tfstate") for _, path in fetched)

    def test_an_empty_prefix_is_an_error_not_zero_results(self, buckets, tmp_path: Path) -> None:
        with pytest.raises(TfstateError, match="no state files"):
            fetch_sources([StateSource(uri="s3://acme-tfstate-stg/nothing/")], tmp_path, client_factory=factory)

    def test_an_unreadable_object_names_the_permission(self, buckets, tmp_path: Path) -> None:
        source = StateSource(uri="s3://acme-tfstate-prod/does-not-exist.tfstate")
        with pytest.raises(SourceError, match="s3:GetObject"):
            fetch_sources([source], tmp_path, client_factory=factory)

    def test_the_error_mentions_the_role_when_one_was_used(self, buckets, tmp_path: Path) -> None:
        source = StateSource(uri="s3://acme-tfstate-prod/nope.tfstate", role_arn="arn:aws:iam::9:role/ro")
        with pytest.raises(SourceError, match="arn:aws:iam::9:role/ro"):
            fetch_sources([source], tmp_path, client_factory=factory)

    def test_a_malformed_uri_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="valid s3:// URI"):
            fetch_sources([StateSource(uri="s3://only-a-bucket")], tmp_path, client_factory=factory)

    def test_a_local_source_needs_no_client(self, tmp_path: Path) -> None:
        fetched = fetch_sources([StateSource(uri=str(FIXTURES / "sample.tfstate"))], tmp_path)
        assert len(fetched) == 1
        assert fetched[0][0].endswith("sample.tfstate")


def test_a_source_describes_itself_with_its_credentials() -> None:
    assert "profile prod" in str(StateSource(uri="s3://b/k", profile="prod"))
    assert "role arn:x" in str(StateSource(uri="s3://b/k", role_arn="arn:x"))


class TestItCannotWriteToYourState:
    """Not "we checked and it doesn't" — "it is unable to, and here is the proof"."""

    def snapshot(self, client) -> dict[str, str]:
        import hashlib

        out = {}
        for bucket in ("acme-tfstate-prod", "acme-tfstate-stg"):
            for item in client.list_objects_v2(Bucket=bucket).get("Contents", []):
                body = client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
                out[f"{bucket}/{item['Key']}"] = hashlib.sha256(body).hexdigest()
        return out

    def test_a_full_fetch_leaves_every_object_byte_identical(self, buckets, tmp_path: Path) -> None:
        before = self.snapshot(buckets)
        fetch_sources(
            [
                StateSource(uri="s3://acme-tfstate-prod/"),
                StateSource(uri="s3://acme-tfstate-stg/main.tfstate"),
            ],
            tmp_path,
            client_factory=factory,
        )
        assert self.snapshot(buckets) == before
        assert before  # the snapshot is not vacuously empty

    def test_no_object_is_added_or_removed(self, buckets, tmp_path: Path) -> None:
        before = set(self.snapshot(buckets))
        fetch_sources([StateSource(uri="s3://acme-tfstate-prod/")], tmp_path, client_factory=factory)
        assert set(self.snapshot(buckets)) == before

    def test_a_write_is_refused_before_it_reaches_the_network(self, buckets) -> None:
        """The guard, not a convention: put_object raises rather than executing."""
        from dora_roi.collectors.aws import AwsError, readonly

        with pytest.raises(AwsError, match="read-only"):
            readonly(buckets, "put_object", Bucket="acme-tfstate-prod", Key="x", Body=b"x")

    def test_every_s3_call_in_the_module_goes_through_the_guard(self) -> None:
        """A new unguarded call would slip past the tests above; this catches it."""
        import inspect
        import re

        from dora_roi.collectors import sources

        source = inspect.getsource(sources)
        direct = re.findall(r"client\.[a-z_0-9]+\(", source)
        assert direct == [], f"unguarded boto3 call(s): {direct}"

    def test_no_dynamodb_client_is_ever_built(self) -> None:
        """Reading state must not take a Terraform lock, or it could block an apply.

        Looks for a client being constructed, not for the word: the module
        docstring says "DynamoDB" precisely to explain that it is left alone, and
        a test that failed on the explanation would be worse than no test.
        """
        import inspect
        import re

        from dora_roi.collectors import sources

        built = re.findall(r"(?:client|resource)\(\s*[\"']dynamodb", inspect.getsource(sources))
        assert built == []


class TestWorkspaces:
    """The S3 backend nests every workspace but `default` under env:/."""

    @pytest.fixture
    def workspaced(self):
        with mock_aws():
            client = boto3.client("s3", region_name=REGION)
            client.create_bucket(Bucket="acme-ws", CreateBucketConfiguration={"LocationConstraint": REGION})
            # A stale default workspace beside two real ones — the layout that
            # made a real scan read 137 KB of leftovers and call it an estate.
            client.put_object(Bucket="acme-ws", Key="acme/be/terraform.tfstate", Body=SECOND.encode())
            client.put_object(Bucket="acme-ws", Key="env:/production/acme/be/terraform.tfstate", Body=STATE.encode())
            client.put_object(Bucket="acme-ws", Key="env:/staging/acme/be/terraform.tfstate", Body=SECOND.encode())
            yield client

    def test_the_workspace_of_a_key_is_read_from_it(self) -> None:
        from dora_roi.collectors.sources import workspace_of

        assert workspace_of("env:/production/a/terraform.tfstate") == "production"
        assert workspace_of("a/terraform.tfstate") == "default"

    def test_several_workspaces_in_reach_is_refused_not_merged(self, workspaced, tmp_path: Path) -> None:
        """Merging them would put production and staging in one register."""
        with pytest.raises(SourceError, match="more than one Terraform workspace"):
            fetch_sources([StateSource(uri="s3://acme-ws/")], tmp_path, client_factory=lambda s: workspaced)

    def test_the_refusal_names_the_workspaces(self, workspaced, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="default, production, staging"):
            fetch_sources([StateSource(uri="s3://acme-ws/")], tmp_path, client_factory=lambda s: workspaced)

    def test_naming_a_workspace_selects_only_that_one(self, workspaced, tmp_path: Path) -> None:
        source = StateSource(uri="s3://acme-ws/", workspace="production")
        fetched = fetch_sources([source], tmp_path, client_factory=lambda s: workspaced)
        assert [uri for uri, _ in fetched] == ["s3://acme-ws/env:/production/acme/be/terraform.tfstate"]

    def test_a_default_workspace_prefix_still_finds_a_named_workspace(self, workspaced, tmp_path: Path) -> None:
        """Pointing at `acme/` and asking for production must not silently read default."""
        source = StateSource(uri="s3://acme-ws/acme/", workspace="production")
        fetched = fetch_sources([source], tmp_path, client_factory=lambda s: workspaced)
        assert all("env:/production/" in uri for uri, _ in fetched)

    def test_default_can_be_asked_for_explicitly(self, workspaced, tmp_path: Path) -> None:
        source = StateSource(uri="s3://acme-ws/", workspace="default")
        fetched = fetch_sources([source], tmp_path, client_factory=lambda s: workspaced)
        assert [uri for uri, _ in fetched] == ["s3://acme-ws/acme/be/terraform.tfstate"]

    def test_an_unknown_workspace_lists_the_real_ones(self, workspaced, tmp_path: Path) -> None:
        source = StateSource(uri="s3://acme-ws/", workspace="prod")
        with pytest.raises(SourceError, match="Found: default, production, staging"):
            fetch_sources([source], tmp_path, client_factory=lambda s: workspaced)

    def test_one_workspace_only_needs_no_declaration(self, buckets, tmp_path: Path) -> None:
        """A bucket with no env:/ at all is unambiguous; do not nag about it."""
        fetched = fetch_sources([StateSource(uri="s3://acme-tfstate-prod/")], tmp_path, client_factory=factory)
        assert len(fetched) == 2

    def test_the_source_says_which_workspace_it_is(self) -> None:
        assert "workspace production" in str(StateSource(uri="s3://b/", workspace="production"))


class TestAwsSweep:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "sources.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_no_aws_block_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_aws_sweep(self._write(tmp_path, "states:\n  - a.tfstate\n")) is None

    def test_an_explicit_account_list_keeps_its_credentials(self, tmp_path: Path) -> None:
        sweep = load_aws_sweep(
            self._write(
                tmp_path,
                "aws:\n"
                "  profile: management\n"
                "  accounts:\n"
                '    - id: "111122223333"\n'
                "      role_arn: arn:aws:iam::111122223333:role/DoraRoiReadOnly\n"
                "    - profile: staging\n",
            )
        )
        assert sweep is not None
        assert sweep.profile == "management"
        assert sweep.accounts[0].role_arn == "arn:aws:iam::111122223333:role/DoraRoiReadOnly"
        assert sweep.accounts[1].profile == "staging"

    def test_the_assume_role_shortcut_is_read(self, tmp_path: Path) -> None:
        sweep = load_aws_sweep(self._write(tmp_path, "aws:\n  assume_role_name: DoraRoiReadOnly\n"))
        assert sweep is not None
        assert sweep.assume_role_name == "DoraRoiReadOnly"

    def test_a_typo_names_the_key_it_did_not_recognise(self, tmp_path: Path) -> None:
        """Silently ignoring an unknown key is how a scan reads less than you asked."""
        with pytest.raises(SourceError, match="assume_role_nmae"):
            load_aws_sweep(self._write(tmp_path, "aws:\n  assume_role_nmae: X\n"))

    def test_an_account_with_neither_profile_nor_role_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="profile.*role_arn"):
            load_aws_sweep(self._write(tmp_path, 'aws:\n  accounts:\n    - id: "111122223333"\n'))

    def test_an_aws_block_that_is_all_comments_is_not_a_sweep(self, tmp_path: Path) -> None:
        """`aws:` with everything commented out parses as None, not as a dict."""
        assert load_aws_sweep(self._write(tmp_path, "aws:\n  # profile: management\n")) is None
