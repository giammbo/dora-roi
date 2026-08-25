"""Terraform / OpenTofu state collector.

What a state file tells you is which *providers* you have configured and how
much you run through each. That is the honest unit of discovery here: a
provider block is a vendor relationship, and the resources under it are the
weight of that relationship. This module extracts nothing else — no vendor
names, no service classification, no LEIs. Those are hypotheses and belong to
``enrichment``.

Three rules shape the parser, and each one exists to avoid saying
something untrue about a register:

* **Format version 4 only.** Older layouts are structurally different, and
  guessing at them would produce a confident, wrong inventory.
* **Managed resources only.** A ``data`` block is a read, not a purchase.
  Counting ``aws_caller_identity`` as a vendor relationship would inflate the
  register with things nobody contracted for.
* **Regions are never guessed.** They come from an explicit ``region``
  attribute, or from the region segment of an ARN, or not at all. A wrong
  country of provision in B_02.02.0130 is worse than a gap the report flags.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from dora_roi.collectors.domains import hosts_in, vendor_for_host

__all__ = [
    "STATE_SUFFIXES",
    "DiscoveredProvider",
    "NON_VENDOR_PROVIDERS",
    "TfstateError",
    "merge_providers",
    "parse_many",
    "parse_state_file",
    "resolve_state_paths",
]

#: What a state file is called on disk. `terraform state pull > x.json` is common
#: enough that plain `.json` would be tempting — and would sweep up every
#: package.json and tsconfig in a repository, so it is not included.
STATE_SUFFIXES = (".tfstate", ".tfstate.json", ".tfstate.backup")

SUPPORTED_STATE_VERSION = 4

#: Providers that contract with nobody. They generate a value locally — a zip, a
#: sleep, a random string — and have no vendor behind them at all. Reporting
#: `hashicorp/random` as an ICT third-party service provider would put a fiction
#: in a regulatory filing, and on a real estate they outnumbered the genuine
#: vendors three to one.
NON_VENDOR_PROVIDERS = frozenset(
    {"random", "null", "local", "time", "archive", "external", "template", "tls", "cloudinit", "terraform"}
)

#: Resource types that hold a DNS record, across the providers that manage DNS.
#: Matched by suffix so `aws_route53_record`, `google_dns_record_set` and
#: `azurerm_dns_cname_record` all qualify without an exhaustive list.
_DNS_RECORD_SUFFIXES = ("_record", "_record_set", "_recordset")

#: Attributes of a DNS record that hold *who you point at*. `name` is absent on
#: purpose: that is your own hostname, not a counterparty, and reading it would
#: let your own zone match the vendor table.
_DNS_VALUE_KEYS = ("records", "rrdatas", "value", "content", "record", "alias", "resource_records")

# Matches the provider reference terraform writes into state, in all the shapes
# it takes: bare, aliased (`...aws"].eu`) and module-scoped
# (`module.data.provider["..."]`). The alias is captured but deliberately not
# used to split providers — an alias is a second configuration of the same
# vendor, not a second vendor.
_PROVIDER_RE = re.compile(
    r'provider\["(?P<registry>[^/"]+)/(?P<namespace>[^/"]+)/(?P<name>[^/"]+)"\](?:\.(?P<alias>[\w-]+))?'
)

# arn:partition:service:region:account:resource — the region is segment 3, and
# it is empty for global services such as S3 and IAM.
_ARN_REGION_INDEX = 3


class TfstateError(Exception):
    """A state file could not be read, or is not something we can parse."""


class DiscoveredProvider(BaseModel):
    """One provider found in state, with the weight of what runs through it."""

    name: str
    namespace: str
    registry: str
    resource_count: int = 0
    resource_types: Counter[str] = Field(default_factory=Counter)
    regions: set[str] = Field(default_factory=set)
    source_files: set[str] = Field(default_factory=set)

    @property
    def address(self) -> str:
        """The fully qualified provider address, e.g. ``registry.terraform.io/hashicorp/aws``."""
        return f"{self.registry}/{self.namespace}/{self.name}"


def parse_state_file(path: str | Path, origin: str | None = None) -> list[DiscoveredProvider]:
    """Discover providers in one state file, ordered by resource count desc.

    ``origin`` is what to record as the evidence for each provider: the s3:// URI
    it was fetched from, when it was. Defaults to the path itself. A register
    that cites a temporary directory nobody can look in is not citing anything.
    """
    path = Path(path)
    origin = origin or str(path)
    document = _load(path)
    _check_version(document, path)

    found: dict[str, DiscoveredProvider] = {}
    for resource in document.get("resources") or []:
        if not isinstance(resource, dict) or resource.get("mode") != "managed":
            continue
        registry, namespace, name = _parse_provider(resource, path)
        if name in NON_VENDOR_PROVIDERS:
            continue
        provider = found.get(name)
        if provider is None:
            provider = DiscoveredProvider(name=name, namespace=namespace, registry=registry)
            provider.source_files.add(origin)
            found[name] = provider

        instances = resource.get("instances") or []
        resource_type = str(resource.get("type") or "")
        provider.resource_count += len(instances)
        if resource_type:
            provider.resource_types[resource_type] += len(instances)
        for instance in instances:
            region = _region_of(instance)
            if region:
                provider.regions.add(region)
            if resource_type.endswith(_DNS_RECORD_SUFFIXES):
                _record_dns_vendors(instance, found, origin, registry)

    return _sorted(found.values())


def _record_dns_vendors(instance: Any, found: dict[str, DiscoveredProvider], origin: str, registry: str) -> None:
    """Add the vendors a DNS record points at.

    This is the channel Terraform's own provider list cannot show you. A zone
    managed by `hashicorp/aws` still names Google as your mail provider, HubSpot
    as your marketing platform and Mailchimp as your sender — relationships that
    exist, process personal data, and belong in the register. Reading only the
    provider block reports one vendor and misses eight.
    """
    if not isinstance(instance, dict):
        return
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return

    for key in _DNS_VALUE_KEYS:
        for value in _strings_under(attributes.get(key)):
            for host in hosts_in(value):
                vendor = vendor_for_host(host)
                if vendor is None:
                    continue
                provider = found.get(vendor)
                if provider is None:
                    # A DNS record says nothing about a registry or a namespace;
                    # claiming one would be inventing provenance.
                    provider = DiscoveredProvider(name=vendor, namespace="", registry="dns", resource_types=Counter())
                    found[vendor] = provider
                provider.source_files.add(origin)
                provider.resource_count += 1
                provider.resource_types["dns_record"] += 1
    _ = registry


def _strings_under(value: Any) -> list[str]:
    """Every string in an attribute, whatever shape it arrived in.

    Record values are a list on Route53, a list on Google, a bare string on
    Cloudflare and a list of objects on Azure. Walking the shape is shorter than
    a branch per provider, and does not silently skip the one nobody tested.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings_under(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings_under(item)]
    return []


def parse_many(paths: Iterable[str | Path], origins: Mapping[Path, str] | None = None) -> list[DiscoveredProvider]:
    """Discover providers across several state files, merged into one inventory.

    ``origins`` maps a local path to what it should be *called* — the s3:// URI a
    fetched file came from. Without it a scan of remote state cites
    /tmp/dora-roi-state-xyz/0/…, which is true for about as long as the process
    lives and useless to anyone reading the register afterwards.
    """
    origins = origins or {}
    return merge_providers(parse_state_file(path, origins.get(Path(path))) for path in paths)


def resolve_state_paths(inputs: Iterable[str | Path]) -> list[Path]:
    """Expand what a person typed into the state files they meant.

    A file is itself. A **directory** is every state file under it, recursively,
    because an organisation keeps one stack per directory and nobody is going to
    type twenty `-s` flags. A **glob** is its matches, for the case where the
    shell did not expand it.

    Returns real paths, deduplicated and sorted, so a run is reproducible and so
    the perimeter the report declares is the list of files actually read — never
    the directory they were found in. Golden rule 5 is about what was looked at.
    """
    found: dict[Path, None] = {}
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            matches = [p for p in sorted(path.rglob("*")) if p.is_file() and _looks_like_state(p)]
            if not matches:
                raise TfstateError(
                    f"no state files under {path}. Looked for {', '.join(STATE_SUFFIXES)} recursively. "
                    f"If your state is exported under another name, pass the files directly with -s."
                )
            found.update(dict.fromkeys(matches))
        elif path.exists():
            found[path] = None
        elif any(character in str(raw) for character in "*?["):
            matches = sorted(Path().glob(str(raw)))
            if not matches:
                raise TfstateError(f"the pattern {raw!r} matched no files.")
            found.update(dict.fromkeys(matches))
        else:
            # Let the parser produce the "not found" message, which already says
            # how to export a state file.
            found[path] = None
    return list(found)


def _looks_like_state(path: Path) -> bool:
    return any(path.name.endswith(suffix) for suffix in STATE_SUFFIXES)


def merge_providers(groups: Iterable[Sequence[DiscoveredProvider]]) -> list[DiscoveredProvider]:
    """Merge per-file results, summing counts and unioning regions and sources.

    Inputs are left untouched: callers keep per-file results for the perimeter
    statement the report has to make (golden rule 5).
    """
    merged: dict[str, DiscoveredProvider] = {}
    for group in groups:
        for provider in group:
            existing = merged.get(provider.name)
            if existing is None:
                merged[provider.name] = provider.model_copy(deep=True)
                continue
            existing.resource_count += provider.resource_count
            existing.resource_types.update(provider.resource_types)
            existing.regions |= provider.regions
            existing.source_files |= provider.source_files
    return _sorted(merged.values())


def _sorted(providers: Iterable[DiscoveredProvider]) -> list[DiscoveredProvider]:
    """Heaviest first; name breaks ties so output is stable run to run."""
    return sorted(providers, key=lambda p: (-p.resource_count, p.name))


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise TfstateError(
            f"state file not found: {path}. Write one with `terraform state pull > state.json` "
            f"(or `tofu state pull`), or point --state at an existing file."
        ) from e
    except OSError as e:
        raise TfstateError(f"could not read state file {path}: {e}") from e

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TfstateError(
            f"{path} is not valid JSON ({e}). A state file must be the JSON that "
            f"`terraform state pull` writes, not a .tf source file or a truncated download."
        ) from e

    if not isinstance(document, dict):
        raise TfstateError(
            f"{path} is valid JSON but not a state file: expected an object, got {type(document).__name__}."
        )
    return document


def _check_version(document: dict[str, Any], path: Path) -> None:
    if "version" not in document:
        raise TfstateError(
            f"{path} has no `version` key, so it is not a Terraform state file. "
            f"Expected state format version {SUPPORTED_STATE_VERSION}."
        )
    version = document["version"]
    if version != SUPPORTED_STATE_VERSION:
        raise TfstateError(
            f"{path} is state format version {version}; only version {SUPPORTED_STATE_VERSION} is supported. "
            f"Upgrade the state with a newer Terraform/OpenTofu, then re-export it with "
            f"`terraform state pull > state.json`."
        )


def _parse_provider(resource: dict[str, Any], path: Path) -> tuple[str, str, str]:
    reference = resource.get("provider")
    match = _PROVIDER_RE.search(reference) if isinstance(reference, str) else None
    if match is None:
        address = f"{resource.get('type', '?')}.{resource.get('name', '?')}"
        raise TfstateError(
            f"{path}: cannot read the provider of resource {address}. Expected a reference like "
            f'provider["registry.terraform.io/hashicorp/aws"], got {reference!r}.'
        )
    return match.group("registry"), match.group("namespace"), match.group("name")


def _region_of(instance: Any) -> str | None:
    """Explicit region attribute, else the ARN's region segment, else nothing."""
    if not isinstance(instance, dict):
        return None
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return None

    region = attributes.get("region")
    if isinstance(region, str) and region:
        return region

    arn = attributes.get("arn")
    if isinstance(arn, str) and arn.startswith("arn:"):
        segments = arn.split(":")
        if len(segments) > _ARN_REGION_INDEX and segments[_ARN_REGION_INDEX]:
            return segments[_ARN_REGION_INDEX]
    return None
