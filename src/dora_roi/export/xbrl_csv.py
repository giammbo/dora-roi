"""xBRL-CSV report package writer.

Everything here is shaped by the official EBA sample package, vendored in
`tests/fixtures/eba/`, and `test_xbrl_csv.py` compares what this produces against
it structurally. Golden rule 6: the sample decides, not our reading of the spec.

Four things the sample settled that no amount of reasoning would have:

* **Column headers carry a `c` prefix.** The header is ``c0010``, not ``0010``.
  Our model aliases stay bare codes and the prefix is added here, at the edge.
* **Closed-list values are QNames.** A filing carries ``eba_TA:S17``, never
  "Cloud services: IaaS". Every enum knows its own QName; this module asks.
* **The archive wraps everything in one directory** named exactly like the zip.
  `META-INF/` and `reports/` do not sit at the root.
* **Filing indicators are upper-case** (`B_05.01`) while the CSV files they refer
  to are lower-case (`b_05.01.csv`).

What this module deliberately does *not* do is decide whether a register is fit
to file. It writes what it is given. :mod:`dora_roi.export.preflight` is where
that question belongs, and the CLI runs it first.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from functools import cache
from importlib import resources
from pathlib import Path
from typing import get_args

from dora_roi.models.enums import IdentifierType
from dora_roi.models.templates import (
    FIELD_CATALOG,
    LEI_PATTERN,
    MODELLED_TEMPLATES,
    TEMPLATE_COLLECTIONS,
    RegisterOfInformation,
    RoIRow,
)

__all__ = [
    "DORA_MODULE_URL",
    "ExportError",
    "PackageName",
    "build_package",
    "write_package",
]


class ExportError(Exception):
    """A register could not be turned into a report package."""


#: What `report.json` extends. Taken from the sample, not from a document.
DORA_MODULE_URL = "http://www.eba.europa.eu/eu/fr/xbrl/crr/fws/dora/4.0/mod/dora.json"
_REPORT_PACKAGE_TYPE = "https://xbrl.org/report-package/2023"
_XBRL_CSV_TYPE = "https://xbrl.org/2021/xbrl-csv"

_LEI_RE = re.compile(LEI_PATTERN)
ISO2_PATTERN = r"^[A-Z]{2}$"
ISO4217_PATTERN = r"^[A-Z]{3}$"
_ISO2_RE = re.compile(ISO2_PATTERN)

#: ISO codes are closed lists too, and the QName carries the code in its suffix:
#: `IT` files as `eba_GA:IT`, `EUR` as `eba_CU:EUR`. Emitting the bare code
#: produces `prefix '_2' is not defined`, which is what the EBA taxonomy told us
#: the first time this exporter was run through a real XBRL processor.
_COUNTRY_PREFIX = "eba_GA"
_CURRENCY_PREFIX = "eba_CU"

#: (template, code) -> the closed list a free-text column really draws from.
#: These fields are typed `str` in the models because their lists run to
#: hundreds of values (see UNVERIFIED_CLOSED_LISTS in models/templates.py), but
#: a filing still needs the QName, so the label is resolved here.
_LABELLED_COLUMNS: dict[tuple[str, str], str] = {
    ("B_01.01", "0040"): "entity_type",
    ("B_01.02", "0040"): "entity_type_b0102",
    ("B_01.02", "0050"): "hierarchy",
    ("B_02.02", "0090"): "termination_reason",
    ("B_04.01", "0030"): "nature_of_entity",
    ("B_06.01", "0020"): "licenced_activity",
    ("B_06.01", "0050"): "criticality",
}


@cache
def _labels(list_name: str) -> dict[str, str]:
    """Case-folded label -> QName.

    Case-insensitive on purpose. The official label is "payment institution",
    lower case, and somebody typing "Payment institution" into a YAML file means
    exactly the right thing. Refusing that would be pedantry charged to the user.
    """
    raw = resources.files("dora_roi.data").joinpath("eba_closed_lists.json").read_text(encoding="utf-8")
    return {value["label"].casefold(): value["qname"] for value in json.loads(raw)[list_name]["values"]}


@cache
def _key_columns() -> dict[str, frozenset[str]]:
    """template -> the columns that identify a row, from the annotated layout.

    Marked ``<Key value>`` in the official workbook. They matter because a row
    made only of keys carries no fact, and xBRL-CSV has nowhere to put it: the
    validator calls it an unmapped parameter column and rejects the instance.
    """
    raw = resources.files("dora_roi.data").joinpath("eba_closed_lists.json").read_text(encoding="utf-8")
    entry = json.loads(raw)["_eba_key_columns"]["columns"]
    return {template: frozenset(codes) for template, codes in entry.items()}


@cache
def _identifier_columns() -> frozenset[tuple[str, str]]:
    """Columns holding a "type of code", which file as an identifier-type QName.

    Found by attribute name rather than listed: every one of them is a
    `type_of_*_code` on some row model, and a new template with one would
    otherwise be silently emitted as a bare `LEI`.
    """
    return frozenset(
        (template, info.alias)
        for template, model in MODELLED_TEMPLATES.items()
        for attribute, info in model.model_fields.items()
        if info.alias is not None and attribute.startswith("type_of") and attribute.endswith("code")
    )


@cache
def _iso_columns() -> tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]:
    """Which columns hold a country and which a currency, read off the models.

    Derived from the `StringConstraints` the row models already carry, so a new
    country column cannot be forgotten here. The constraint lives inside the
    Optional — the field is `CountryCode | None` — which is why this digs through
    the union rather than reading `FieldInfo.metadata`, where it is not.
    """
    countries: set[tuple[str, str]] = set()
    currencies: set[tuple[str, str]] = set()
    for template, model in MODELLED_TEMPLATES.items():
        for info in model.model_fields.values():
            if info.alias is None:
                continue
            patterns = {
                getattr(piece, "pattern", None)
                for argument in get_args(info.annotation)
                for piece in getattr(argument, "__metadata__", ())
            }
            if ISO2_PATTERN in patterns:
                countries.add((template, info.alias))
            elif ISO4217_PATTERN in patterns:
                currencies.add((template, info.alias))
    return frozenset(countries), frozenset(currencies)


#: The sample's parameters.csv, whose values are not free-form.
_DECIMALS_INTEGER = "0"
_DECIMALS_MONETARY = "-3"


@dataclass(frozen=True)
class PackageName:
    """The pieces of the filename, which is itself a validated artefact.

    A package whose name is wrong is rejected before anything inside it is read,
    so the name is built from parts and checked, never concatenated by a caller.
    """

    lei: str
    consolidated: bool
    country: str
    reference_date: date
    timestamp: str

    def __post_init__(self) -> None:
        if not _LEI_RE.fullmatch(self.lei):
            raise ExportError(f"the package name needs a valid LEI, got {self.lei!r} (ISO 17442).")
        if not _ISO2_RE.fullmatch(self.country):
            raise ExportError(f"the package name needs an upper-case ISO 3166-1 alpha-2 country, got {self.country!r}.")
        if not re.fullmatch(r"\d{17}", self.timestamp):
            raise ExportError(
                f"the timestamp must be 17 digits as in the EBA sample (yyyyMMddHHmmssSSS), got {self.timestamp!r}."
            )

    @property
    def scope(self) -> str:
        return "CON" if self.consolidated else "IND"

    def __str__(self) -> str:
        return (
            f"{self.lei}.{self.scope}_{self.country}_DORA010100_DORA_{self.reference_date.isoformat()}_{self.timestamp}"
        )


def build_package(
    roi: RegisterOfInformation,
    name: PackageName,
    *,
    base_currency: str = "EUR",
    generating_software: str | None = None,
) -> dict[str, str]:
    """The whole package as ``path -> text``, ready to be zipped or inspected.

    Returning a mapping rather than writing straight to disk keeps the layout
    testable without a filesystem, and keeps :func:`write_package` down to the
    part that can actually fail on I/O.
    """
    if not re.fullmatch(ISO4217_PATTERN, base_currency):
        raise ExportError(f"base currency must be an ISO 4217 code, got {base_currency!r}.")

    root = str(name)
    files = {
        f"{root}/META-INF/reportPackage.json": _report_package_json(),
        f"{root}/reports/report.json": _report_json(generating_software),
        f"{root}/reports/parameters.csv": _parameters_csv(name, base_currency),
        f"{root}/reports/FilingIndicators.csv": _filing_indicators_csv(roi),
    }
    for template in FIELD_CATALOG:
        files[f"{root}/reports/{template.lower()}.csv"] = _table_csv(template, _rows_for(roi, template))
    return files


def write_package(
    roi: RegisterOfInformation,
    name: PackageName,
    output: str | Path,
    *,
    base_currency: str = "EUR",
    generating_software: str | None = None,
) -> Path:
    """Write the zip. Returns the path written."""
    files = build_package(roi, name, base_currency=base_currency, generating_software=generating_software)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{name}.zip"
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, content in files.items():
                archive.writestr(path, content)
    except OSError as e:
        raise ExportError(f"could not write {target}: {e}") from e
    return target


# -- the pieces -------------------------------------------------------------


def _report_package_json() -> str:
    return json.dumps({"documentInfo": {"documentType": _REPORT_PACKAGE_TYPE}}, indent=2) + "\n"


def _report_json(generating_software: str | None) -> str:
    document: dict[str, object] = {"documentType": _XBRL_CSV_TYPE, "extends": [DORA_MODULE_URL]}
    payload: dict[str, object] = {"documentInfo": document}
    if generating_software:
        # Optional, and named exactly as the EBA filing rules spell it.
        payload["eba:generatingSoftwareInformation"] = generating_software
    return json.dumps(payload, indent=4) + "\n"


def _parameters_csv(name: PackageName, base_currency: str) -> str:
    return _csv(
        ["name", "value"],
        [
            ["entityID", f"rs:{name.lei}.{name.scope}"],
            ["refPeriod", name.reference_date.isoformat()],
            ["baseCurrency", f"iso4217:{base_currency}"],
            ["decimalsInteger", _DECIMALS_INTEGER],
            ["decimalsMonetary", _DECIMALS_MONETARY],
        ],
    )


def _filing_indicators_csv(roi: RegisterOfInformation) -> str:
    """One row per template, upper-case, saying whether it is reported.

    A template with no rows is reported ``false`` rather than omitted: silence
    and "we have nothing for this" are different claims, and only one of them is
    true.
    """
    return _csv(
        ["templateID", "reported"],
        [[template, "true" if _rows_for(roi, template) else "false"] for template in sorted(FIELD_CATALOG)],
    )


def _table_csv(template: str, rows: list[RoIRow]) -> str:
    """One CSV per template. Header order follows the catalog, which follows the EBA."""
    codes = [spec.code for spec in FIELD_CATALOG[template]]
    header = [f"c{code}" for code in codes]
    if not rows:
        return _csv(header, [])

    by_code = {
        info.alias: attribute
        for attribute, info in MODELLED_TEMPLATES[template].model_fields.items()
        if info.alias is not None
    }
    keys = _key_columns().get(template, frozenset())
    body = []
    for position, row in enumerate(rows, start=1):
        cells = {code: _cell(getattr(row, by_code[code], None), template, code) for code in codes}
        if keys and not any(value for code, value in cells.items() if code not in keys):
            filled = "nothing at all" if not any(cells.values()) else "only key columns"
            raise ExportError(
                f"{template} row {position} has {filled} filled, which xBRL-CSV cannot represent: a key "
                f"with no fact attached maps to nothing and the package is rejected. Either fill the row "
                f"through the overlay or remove it. Keys here are {', '.join(sorted(keys))}."
            )
        body.append([cells[code] for code in codes])
    return _csv(header, body)


def _rows_for(roi: RegisterOfInformation, template: str) -> list[RoIRow]:
    held = getattr(roi, TEMPLATE_COLLECTIONS[template], None)
    if held is None:
        return []
    return list(held) if isinstance(held, list) else [held]


def _cell(value: object, template: str = "", code: str = "") -> str:
    """One value, in the form a filing carries it.

    Closed lists are the whole difficulty. Every one of them holds a
    human-readable label in this project, and a filing needs the EBA's QName —
    and "closed list" turns out to include the things that look like plain data:
    a country, a currency, an entity type. Emitting the bare code produces a
    package that opens fine and validates to nothing.
    """
    if value is None:
        return ""

    countries, currencies = _iso_columns()
    if (template, code) in countries and isinstance(value, str):
        return f"{_COUNTRY_PREFIX}:{value}"
    if (template, code) in currencies and isinstance(value, str):
        return f"{_CURRENCY_PREFIX}:{value}"

    if (template, code) in _identifier_columns() and isinstance(value, str):
        try:
            return IdentifierType(value).qname
        except ValueError:
            qname = _labels("identifier_type").get(value.casefold())
            if qname is None:
                raise ExportError(
                    f"{template}.{code} is a type of identification code and {value!r} is not one. "
                    f"Use one of: {', '.join(m.value for m in IdentifierType)}."
                ) from None
            return qname

    list_name = _LABELLED_COLUMNS.get((template, code))
    if list_name and isinstance(value, str):
        qname = _labels(list_name).get(value.casefold())
        if qname is None:
            raise ExportError(
                f"{template}.{code} is a closed list and {value!r} is not one of its values. "
                f"Use the official wording; `dora-roi check` lists what is accepted."
            )
        return qname
    if isinstance(value, Enum):
        qname = getattr(value, "qname", None)
        if qname is None:
            raise ExportError(
                f"{type(value).__name__}.{value.name} has no EBA QName, so it cannot be filed. "
                f"Closed lists must be reconciled against the EBA list of possible values."
            )
        return str(qname)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        # No normalize(): it would turn 120000.50 into 120000.5 and drop a
        # significant digit from a monetary amount.
        return format(value, "f")
    return str(value)


def _csv(header: list[str], rows: Iterable[list[str]]) -> str:
    """CRLF and no trailing blank line, as the sample writes them."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()
