"""Terraform provider to ICT third-party service provider.

This module turns "there is an `aws` provider in your state" into "the
counterparty is probably Amazon Web Services EMEA SARL, in Luxembourg,
providing S17 and S18". Every word of that is a hypothesis, so every value it
produces is ``INFERRED`` with ``source="mapping"`` — golden rule 1 leaves no
room for anything else here, and golden rule 4 makes the S-code the user's call
in the end, not ours.

The packaged table is a starting point that gets two things structurally wrong
for somebody: which legal entity you actually contract with, and how narrow
your use of a provider is. Both are fixed with a user YAML, deep-merged per key
so overriding ``hq_country`` alone does not cost you the vendor name, the
service codes and the notes.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from dora_roi.collectors.tfstate import DiscoveredProvider
from dora_roi.models.enums import FieldStatus, ICTServiceType, TypeOfPerson
from dora_roi.models.templates import ThirdPartyProvider

__all__ = ["MappingError", "ProviderMapping", "load_mapping", "provider_to_tpp"]

_PACKAGED_MAPPING = "provider_mapping.yaml"
_SOURCE = "mapping"


class MappingError(Exception):
    """The provider mapping could not be read, or says something impossible."""


class ProviderMapping(BaseModel):
    """One row of the mapping table: who the vendor probably is, and what they probably do."""

    model_config = ConfigDict(extra="forbid")

    vendor: str
    hq_country: str
    services: list[ICTServiceType]
    notes: str | None = None

    @field_validator("hq_country")
    @classmethod
    def _iso2(cls, value: str) -> str:
        if len(value) != 2 or not value.isalpha() or not value.isupper():
            raise ValueError(f"hq_country must be an upper-case ISO 3166-1 alpha-2 code, got {value!r}")
        return value


def load_mapping(user_file: str | Path | None = None) -> dict[str, ProviderMapping]:
    """Packaged defaults, with ``user_file`` deep-merged over them per key."""
    merged = _read_packaged()
    if user_file is not None:
        for provider, overrides in _read_user(Path(user_file)).items():
            base = merged.get(provider, {})
            merged[provider] = {**base, **overrides}
    return _validate(merged)


def provider_to_tpp(provider: DiscoveredProvider, mapping: dict[str, ProviderMapping]) -> ThirdPartyProvider:
    """Build the B_05.01 row for a discovered provider. Nothing here is FILLED.

    An unmapped provider still produces a row. Dropping it would hide a real
    vendor relationship — the state file is evidence that *something* is
    contracted — so the Terraform provider name goes in as a weak hint, marked
    INFERRED and noted for what it is, and the gap report takes it from there.
    """
    row = ThirdPartyProvider(source_key=provider.name)
    entry = mapping.get(provider.name)

    if entry is None:
        row.legal_name = provider.name
        row.mark(
            "legal_name",
            FieldStatus.INFERRED,
            source=_SOURCE,
            note=(
                f"no mapping for Terraform provider {provider.name!r}: this is the provider name, "
                f"not a legal name. Identify the counterparty and add it to your mapping YAML."
            ),
        )
        return row

    row.legal_name = entry.vendor
    row.mark("legal_name", FieldStatus.INFERRED, source=_SOURCE, note=entry.notes)

    # B_05.01.0060 is the name in Latin alphabet; for these vendors it is the
    # legal name itself. Still INFERRED, because the legal name it mirrors is.
    row.name_latin = entry.vendor
    row.mark("name_latin", FieldStatus.INFERRED, source=_SOURCE, note="mirrors the legal name")

    row.headquarters_country = entry.hq_country
    row.mark("headquarters_country", FieldStatus.INFERRED, source=_SOURCE, note=entry.notes)

    # Every provider in the table is a company. Recorded as an assumption
    # rather than a fact, because an individual acting in a business capacity
    # is a legal possibility the tool cannot rule out from state alone.
    row.person_type = TypeOfPerson.LEGAL_PERSON
    row.mark(
        "person_type", FieldStatus.INFERRED, source=_SOURCE, note="assumed: providers in the mapping are companies"
    )

    return row


def _read_packaged() -> dict[str, dict[str, Any]]:
    try:
        raw = resources.files("dora_roi.data").joinpath(_PACKAGED_MAPPING).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as e:  # pragma: no cover - packaging failure
        raise MappingError(f"the packaged provider mapping is missing from the installation: {e}") from e
    return _parse(raw, f"packaged {_PACKAGED_MAPPING}")


def _read_user(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise MappingError(f"mapping file not found: {path}") from e
    except OSError as e:
        raise MappingError(f"could not read mapping file {path}: {e}") from e
    return _parse(raw, str(path))


def _parse(raw: str, origin: str) -> dict[str, dict[str, Any]]:
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise MappingError(f"{origin} is not valid YAML: {e}") from e

    if document is None:
        return {}
    if not isinstance(document, dict):
        raise MappingError(f"{origin} must be a mapping of provider name to its entry, got {type(document).__name__}.")
    for provider, entry in document.items():
        if not isinstance(entry, dict):
            raise MappingError(f"{origin}: entry for {provider!r} must be a mapping, got {type(entry).__name__}.")
    return document


def _validate(merged: dict[str, dict[str, Any]]) -> dict[str, ProviderMapping]:
    """Turn the merged dicts into models, naming the provider when one is wrong."""
    result: dict[str, ProviderMapping] = {}
    for provider, entry in merged.items():
        try:
            result[provider] = ProviderMapping(**entry)
        except ValidationError as e:
            raise MappingError(f"invalid mapping entry for {provider!r}: {_explain(e)}") from e
        except TypeError as e:
            raise MappingError(f"invalid mapping entry for {provider!r}: {e}") from e
    return result


def _explain(error: ValidationError) -> str:
    """Pydantic's report, flattened to something a user can act on."""
    parts = []
    for item in error.errors():
        location = ".".join(str(piece) for piece in item["loc"]) or "entry"
        given = item.get("input")
        parts.append(f"{location}: {item['msg']} (got {given!r})")
    return "; ".join(parts)
