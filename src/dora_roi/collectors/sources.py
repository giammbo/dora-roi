"""Where the state files actually live.

`terraform state pull` assumes you are already in the right directory, with the
right backend configured and the right credentials loaded. That is fine for one
stack. For a financial entity with five stacks in five S3 buckets across five
AWS accounts it means five manual exports before the tool has even started, and
the register it produces is only as current as the oldest of them.

So a source is a URI plus the credentials needed to read it, and a run can carry
several with different credentials each. The read-only IAM policy anticipated
this: it already reserves `s3:GetObject` scoped to the state bucket.

Read-only, and structurally so: every S3 call goes through
:func:`dora_roi.collectors.aws.readonly`, which refuses anything that is not a
List, Describe, Get or Search *before it reaches the network*. Writing
`put_object` here does not fail in review or in production — it fails at the
call. Objects are copied to a scratch directory and parsed from there.

Nothing touches the DynamoDB lock table either, so a scan never takes a
Terraform lock and can never block an apply that is running beside it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dora_roi.collectors.aws import readonly
from dora_roi.collectors.tfstate import STATE_SUFFIXES, TfstateError, resolve_state_paths

__all__ = [
    "WORKSPACE_PREFIX",
    "AwsAccount",
    "AwsSweep",
    "SourceError",
    "StateSource",
    "fetch_sources",
    "load_aws_sweep",
    "load_sources",
    "workspace_of",
]

_S3_URI = re.compile(r"^s3://(?P<bucket>[^/]+)/(?P<key>.*)$")

#: How the Terraform S3 backend lays out workspaces. The default workspace keeps
#: the bare key; every other one is nested under this prefix. Miss it and a scan
#: reads the default workspace — usually stale leftovers — and reports success.
WORKSPACE_PREFIX = "env:/"
_WORKSPACE_KEY = re.compile(r"^env:/(?P<workspace>[^/]+)/")


class SourceError(Exception):
    """A source list could not be read, or a source could not be fetched."""


@dataclass(frozen=True)
class StateSource:
    """One place state lives, and how to get at it.

    ``profile`` and ``role_arn`` are per source on purpose. Five buckets in five
    accounts need five credentials, and a single global ``--aws-profile`` would
    quietly read whichever ones it could and silently skip the rest — producing a
    register that looks complete and is not.
    """

    uri: str
    profile: str | None = None
    role_arn: str | None = None
    workspace: str | None = None

    @property
    def is_s3(self) -> bool:
        return self.uri.startswith("s3://")

    def __str__(self) -> str:
        where = f"{self.uri} [workspace {self.workspace}]" if self.workspace else self.uri
        if self.role_arn:
            return f"{where} (role {self.role_arn})"
        if self.profile:
            return f"{where} (profile {self.profile})"
        return where


@dataclass(frozen=True)
class AwsAccount:
    """One account to sweep, and how to get into it."""

    id: str | None = None
    profile: str | None = None
    role_arn: str | None = None


@dataclass(frozen=True)
class AwsSweep:
    """Which accounts the vendor channels read, and with whose credentials.

    ``accounts`` is the explicit form and says exactly what will be read.
    ``assume_role_name`` is the shortcut for an organisation too large to list
    by hand: the role is tried in every account Organizations reports. The
    shortcut is only honest because a role that is missing in seven accounts
    out of twenty is recorded as refused rather than skipped in silence.
    """

    profile: str | None = None
    accounts: tuple[AwsAccount, ...] = ()
    assume_role_name: str | None = None


_SWEEP_KEYS = frozenset({"profile", "accounts", "assume_role_name"})
_ACCOUNT_KEYS = frozenset({"id", "profile", "role_arn"})


def _document(path: str | Path) -> Any:
    """Read a sources YAML file and parse it. Every failure becomes a `SourceError`.

    Shared by :func:`load_sources` and :func:`load_aws_sweep`: they read the same
    file, and reading it two different ways would give two different error
    messages for one problem.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise SourceError(f"sources file not found: {path}") from e
    except OSError as e:
        raise SourceError(f"could not read {path}: {e}") from e

    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise SourceError(f"{path} is not valid YAML: {e}") from e


def load_sources(path: str | Path) -> list[StateSource]:
    """Read a sources YAML: a list of URIs, each with optional credentials."""
    document = _document(path)

    if document is None:
        return []
    if not isinstance(document, dict) or "states" not in document:
        raise SourceError(f"{path} must be a mapping with a `states:` list. See `dora-roi sources init`.")

    entries = document["states"]
    if not isinstance(entries, list):
        raise SourceError(f"{path}: `states` must be a list, got {type(entries).__name__}.")

    sources: list[StateSource] = []
    for position, entry in enumerate(entries, start=1):
        if isinstance(entry, str):
            sources.append(StateSource(uri=entry))
            continue
        if not isinstance(entry, dict) or "uri" not in entry:
            raise SourceError(f"{path}: entry {position} needs a `uri`.")
        unknown = set(entry) - {"uri", "profile", "role_arn", "workspace"}
        if unknown:
            raise SourceError(f"{path}: entry {position} has unknown key(s) {', '.join(sorted(unknown))}.")
        sources.append(
            StateSource(
                uri=str(entry["uri"]),
                profile=entry.get("profile"),
                role_arn=entry.get("role_arn"),
                workspace=entry.get("workspace"),
            )
        )
    return sources


def load_aws_sweep(path: str | Path) -> AwsSweep | None:
    """Read the ``aws:`` block of a sources YAML, if it has one."""
    document = _document(path)
    block = document.get("aws") if isinstance(document, dict) else None
    if not isinstance(block, dict):
        # `aws:` with everything commented out parses as None, and an absent
        # block and an empty one mean the same thing: no sweep.
        return None

    unknown = set(block) - _SWEEP_KEYS
    if unknown:
        raise SourceError(f"unknown key(s) under `aws:` in {path}: {', '.join(sorted(unknown))}")

    accounts: list[AwsAccount] = []
    for entry in block.get("accounts") or []:
        if not isinstance(entry, dict):
            raise SourceError(f"each entry under `aws.accounts` must be a mapping, got {entry!r}")
        strange = set(entry) - _ACCOUNT_KEYS
        if strange:
            raise SourceError(f"unknown key(s) under `aws.accounts` in {path}: {', '.join(sorted(strange))}")
        if not entry.get("profile") and not entry.get("role_arn"):
            raise SourceError(
                f"account {entry.get('id', '?')} in {path} has neither `profile` nor `role_arn`: "
                f"dora-roi will not guess which credentials to read an account with."
            )
        accounts.append(AwsAccount(id=entry.get("id"), profile=entry.get("profile"), role_arn=entry.get("role_arn")))

    return AwsSweep(
        profile=block.get("profile"),
        accounts=tuple(accounts),
        assume_role_name=block.get("assume_role_name"),
    )


def fetch_sources(
    sources: Iterable[StateSource],
    into: Path,
    *,
    client_factory: Any | None = None,
    excluded: list[str] | None = None,
) -> list[tuple[str, Path]]:
    """Resolve every source to local files. Returns ``(uri, local path)`` pairs.

    The URI travels alongside the path because the perimeter has to name where
    state came *from*. A report that said it read `/tmp/xyz/0/main.tfstate` would
    be true and useless.

    ``excluded`` collects what was in reach and deliberately not taken — the
    other Terraform workspaces, mostly. An auditor who finds out on their own
    that four more environments existed wonders what else went unmentioned;
    "excluded on purpose, this register describes production" is a complete
    answer, and only the fetch knows enough to give it.
    """
    resolved: list[tuple[str, Path]] = []
    for index, source in enumerate(sources):
        if not source.is_s3:
            for path in resolve_state_paths([source.uri]):
                resolved.append((str(path), path))
            continue
        target = into / str(index)
        target.mkdir(parents=True, exist_ok=True)
        resolved.extend(_fetch_s3(source, target, client_factory, excluded))
    return resolved


def _fetch_s3(
    source: StateSource, into: Path, client_factory: Any | None, excluded: list[str] | None = None
) -> list[tuple[str, Path]]:
    match = _S3_URI.match(source.uri)
    if match is None:
        raise SourceError(f"{source.uri!r} is not a valid s3:// URI. Expected s3://bucket/key or s3://bucket/prefix/.")
    bucket, key = match.group("bucket"), match.group("key")
    client = client_factory(source) if client_factory else _s3_client(source)

    if key.endswith("/") or not key:
        keys = _keys_for_prefix(client, bucket, key, source)
        if excluded is not None and source.workspace:
            excluded.extend(_other_workspaces(client, bucket, key, source))
    else:
        keys = [key]

    fetched: list[tuple[str, Path]] = []
    for one in keys:
        # Mirror the key, do not flatten to its basename. `prod/terraform.tfstate`
        # and `staging/terraform.tfstate` is the commonest S3 layout there is, and
        # a shared basename would have one silently overwrite the other: one stack
        # counted twice, the other lost, and nothing in the output to show it.
        destination = into / one
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            body = readonly(client, "get_object", Bucket=bucket, Key=one)["Body"].read()
        except Exception as e:
            raise SourceError(
                f"could not read s3://{bucket}/{one}: {e}. The caller needs s3:GetObject on that object"
                + (f" through {source.role_arn}." if source.role_arn else ".")
            ) from e
        destination.write_bytes(body)
        fetched.append((f"s3://{bucket}/{one}", destination))
    return fetched


def _list_keys(client: Any, bucket: str, prefix: str, source: StateSource) -> list[str]:
    """Every state file under a prefix, paginated, for one workspace.

    Workspaces are the trap. The S3 backend keeps the default workspace at the
    bare key and every other one under ``env:/<name>/``, so listing a prefix that
    looks like the whole estate quietly returns whichever workspace happens to
    sit there — usually `default`, usually stale — and the scan reports success
    over an empty register.

    Worse, listing the *bucket* would sweep up every workspace at once and merge
    production with staging into one register. So when several are in reach and
    the source has not said which, this refuses and names them. Guessing an
    environment is not a thing a compliance tool gets to do.
    """
    keys: list[str] = []
    token: str | None = None
    while True:
        try:
            page = readonly(
                client,
                "list_objects_v2",
                Bucket=bucket,
                Prefix=prefix,
                **({"ContinuationToken": token} if token else {}),
            )
        except Exception as e:
            raise SourceError(
                f"could not list s3://{bucket}/{prefix}: {e}. The caller needs s3:ListBucket on that bucket."
            ) from e
        keys.extend(
            item["Key"]
            for item in page.get("Contents", [])
            if any(item["Key"].endswith(suffix) for suffix in STATE_SUFFIXES)
        )
        token = page.get("NextContinuationToken")
        if not token:
            break

    keys = _one_workspace(keys, bucket, prefix, source)
    if not keys:
        raise TfstateError(
            f"no state files under s3://{bucket}/{prefix} — looked for {', '.join(STATE_SUFFIXES)}. "
            f"Point the URI at an object directly if your state is stored under another name."
        )
    return sorted(keys)


def _keys_for_prefix(client: Any, bucket: str, key: str, source: StateSource) -> list[str]:
    """Where a workspace's state actually lives, which is rarely where you pointed.

    Asking for `production` under `acme/` has to look under
    `env:/production/acme/` — that is where the backend put it. Searching the
    literal prefix first would find the default workspace's stale copy and stop
    there, which is exactly how a scan reads 137 KB of leftovers and calls it an
    estate.
    """
    if source.workspace and source.workspace != "default" and not key.startswith(WORKSPACE_PREFIX):
        nested = f"{WORKSPACE_PREFIX}{source.workspace}/{key}"
        try:
            return _list_keys(client, bucket, nested, source)
        except (SourceError, TfstateError):
            pass  # not nested there; fall through and filter the literal prefix
    return _list_keys(client, bucket, key, source)


def _other_workspaces(client: Any, bucket: str, key: str, source: StateSource) -> list[str]:
    """Which workspaces were in reach and not taken. Named, so nobody has to ask."""
    root = key.split(WORKSPACE_PREFIX)[0]
    try:
        page = readonly(client, "list_objects_v2", Bucket=bucket, Prefix=root, MaxKeys=1000)
    except Exception:  # noqa: BLE001 - a perimeter note is never worth failing a scan
        return []
    seen = {
        workspace_of(item["Key"])
        for item in page.get("Contents", [])
        if any(item["Key"].endswith(suffix) for suffix in STATE_SUFFIXES)
    }
    return [
        f"s3://{bucket}/ workspace {name!r} (not selected; this run reads {source.workspace!r})"
        for name in sorted(seen - {source.workspace})
    ]


def workspace_of(key: str) -> str:
    """Which Terraform workspace a key belongs to. ``default`` when un-nested."""
    match = _WORKSPACE_KEY.match(key)
    return match.group("workspace") if match else "default"


def _one_workspace(keys: list[str], bucket: str, prefix: str, source: StateSource) -> list[str]:
    if source.workspace:
        chosen = [k for k in keys if workspace_of(k) == source.workspace]
        if not chosen:
            found = sorted({workspace_of(k) for k in keys})
            raise SourceError(
                f"workspace {source.workspace!r} has no state under s3://{bucket}/{prefix}. "
                f"Found: {', '.join(found) or 'nothing'}."
            )
        return chosen

    workspaces = sorted({workspace_of(k) for k in keys})
    if len(workspaces) > 1:
        raise SourceError(
            f"s3://{bucket}/{prefix} holds more than one Terraform workspace: {', '.join(workspaces)}. "
            f"Merging them would put production and staging in the same register. Add `workspace:` to "
            f"the source — one entry per workspace if you really want several."
        )
    return keys


def _s3_client(source: StateSource) -> Any:
    try:
        import boto3
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise SourceError(
            "reading state from S3 needs the `aws` extra: install with `uv tool install 'dora-roi[aws]'`."
        ) from e

    session = boto3.Session(profile_name=source.profile)
    if not source.role_arn:
        return session.client("s3")
    try:
        assumed = session.client("sts").assume_role(RoleArn=source.role_arn, RoleSessionName="dora-roi-readonly")[
            "Credentials"
        ]
    except Exception as e:
        raise SourceError(f"could not assume {source.role_arn}: {e}") from e
    return boto3.Session(
        aws_access_key_id=assumed["AccessKeyId"],
        aws_secret_access_key=assumed["SecretAccessKey"],
        aws_session_token=assumed["SessionToken"],
    ).client("s3")
