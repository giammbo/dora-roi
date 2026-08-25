"""Vendors that leave a footprint in an AWS account but no trace in your code.

Terraform state answers "what did you declare". This module answers a
different question — "who else is in here" — and the two disagree more often
than anyone expects: on the estate this tool was first proved against, nine of
the ten vendors found appeared in no ``required_providers`` block at all.

Four channels are planned here, and only one of them is authoritative. AWS
Marketplace charges carry the seller's legal name from a billing API, which is
a fact — the same class of source as the expense figure in :mod:`.aws`.
Identity providers, EventBridge partner sources and cross-account trust
policies (later channels in this module) carry a hostname or an account
number, which is a hypothesis about who you contracted with — and this module
never upgrades a hypothesis into a fact. A trusted account this tool cannot
name does not become a vendor with an invented name; it becomes a declared
unknown, which is worth more to an auditor than a guess dressed up as data.

Two exceptions, two different failure modes:

* :class:`ClickopsError` is a configuration mistake — the ``aws`` extra is not
  installed, a channel was pointed at something it cannot parse. It is ours to
  fix, so it raises.
* Everything else AWS can say no to — a missing IAM permission, a throttled
  call, an unreachable region — is a runtime denial. Nothing here raises for
  one of those: it is appended to the caller's ``refused`` list and the scan
  carries on, because an empty channel and a refused channel are opposite
  claims about the world and only one of them is ours to make.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from dora_roi.collectors.aws import AwsError, annual_window, readonly
from dora_roi.collectors.tfstate import DiscoveredProvider
from dora_roi.naming import vendor_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_ce.client import CostExplorerClient
else:
    CostExplorerClient = Any

__all__ = ["ClickopsError", "VendorFact", "collect_marketplace"]

#: Cost Explorer's name for the billing entity that groups every Marketplace
#: seller's line items. It is also the placeholder AWS uses for a Marketplace
#: charge it cannot attribute to a legal entity — that placeholder is not a
#: seller and must never be reported as one.
_MARKETPLACE = "AWS Marketplace"
_METRIC = "UnblendedCost"


class ClickopsError(Exception):
    """A configuration mistake. Runtime denials degrade instead of raising."""


@dataclass(frozen=True)
class VendorFact:
    """Something authoritative about a vendor, on its way to a FILLED field."""

    key: str
    legal_name: str
    annual_spend: Decimal | None
    currency: str | None
    source: str


def collect_marketplace(
    *,
    profile: str | None = None,
    refused: list[str],
    client: CostExplorerClient | None = None,
    today: date | None = None,
) -> tuple[list[DiscoveredProvider], list[VendorFact]]:
    """Third-party sellers billed through AWS Marketplace, for the calling account.

    Called once per run, not once per account: Cost Explorer answers for the
    account behind the credentials, and putting this inside a per-account
    sweep would ask the same question N times and risk summing one bill N
    times over.

    ``client`` and ``today`` exist so tests inject a stubbed Cost Explorer
    client and a fixed clock, the same convention :func:`.aws.collect_annual_expense`
    uses — nothing here monkeypatches a private constructor.
    """
    client = client if client is not None else _ce_client(profile)
    start, end = annual_window(today)

    try:
        response = readonly(
            client,
            "get_cost_and_usage",
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=[_METRIC],
            Filter={"Dimensions": {"Key": "BILLING_ENTITY", "Values": [_MARKETPLACE]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "LEGAL_ENTITY_NAME"}],
        )
        totals, currencies = _totals_by_seller(response)
    except AwsError as e:
        refused.append(f"marketplace: {e}")
        return [], []
    except Exception as e:  # noqa: BLE001 - any other AWS failure degrades this channel too
        refused.append(f"marketplace: ce:GetCostAndUsage unavailable ({type(e).__name__}: {e})")
        return [], []

    providers: list[DiscoveredProvider] = []
    facts: list[VendorFact] = []
    for legal_name, spend in sorted(totals.items()):
        key = vendor_key(legal_name)
        providers.append(
            DiscoveredProvider(
                name=key,
                namespace="aws-marketplace",
                registry="aws-marketplace",
                resource_count=1,
                resource_types=Counter({"marketplace_subscription": 1}),
                source_files={"aws:ce"},
            )
        )
        facts.append(
            VendorFact(
                key=key,
                legal_name=legal_name,
                annual_spend=spend,
                currency=currencies.get(legal_name),
                source="aws:ce",
            )
        )
    return providers, facts


def _totals_by_seller(response: dict[str, Any]) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Sum each seller's monthly lines. Currency is tracked per seller, not
    globally: two Marketplace sellers billing in different currencies must
    never be added together under one label, the way a single global currency
    variable would risk doing.
    """
    totals: dict[str, Decimal] = {}
    currencies: dict[str, str] = {}
    for window in response.get("ResultsByTime", []):
        for group in window.get("Groups", []):
            keys = group.get("Keys") or []
            legal_name = keys[0] if keys else ""
            if not legal_name or legal_name == _MARKETPLACE:
                # No legal entity attributed, or the billing-entity placeholder
                # itself: neither is a seller, and reporting one as a vendor
                # would invent a legal name nobody billed under.
                continue
            metric = group.get("Metrics", {}).get(_METRIC, {})
            unit = metric.get("Unit")
            if unit:
                currencies.setdefault(legal_name, unit)
            totals[legal_name] = totals.get(legal_name, Decimal(0)) + _amount(metric.get("Amount"))
    return totals, currencies


def _amount(raw: Any) -> Decimal:
    """Parse a Cost Explorer amount, or raise.

    Defaulting an unparseable amount to zero would tell the register a vendor
    is free when the truth is "we could not read the bill" — exactly the
    invented-value golden rule this module exists to hold the line on. Raising
    here degrades the whole channel to a refusal instead, the same as a denied
    call: both are "we cannot say", never "the answer is zero".
    """
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError) as e:
        raise AwsError(f"Cost Explorer returned an unparseable Marketplace amount: {raw!r}") from e


def _ce_client(profile: str | None) -> CostExplorerClient:
    try:
        import boto3
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise ClickopsError(
            "the AWS collector needs the `aws` extra: install with `uv tool install 'dora-roi[aws]'`."
        ) from e
    # Cost Explorer is a global service with a us-east-1 endpoint.
    return boto3.Session(profile_name=profile).client("ce", region_name="us-east-1")
