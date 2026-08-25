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

Two exceptions name two different failure modes, though both end up in the
same place:

* :class:`ClickopsError` names a configuration mistake — the ``aws`` extra is
  not installed, a channel is pointed at something it cannot parse — distinctly
  from a runtime denial, so a human reading ``refused`` can tell "fix your
  setup" apart from "AWS said no".
* Everything else AWS (or ``boto3``) can say no to — a missing IAM permission,
  a mistyped profile, a throttled call, an unreachable region — is a runtime
  denial.

Neither one is allowed to raise out of a collector function in this module:
both are appended to the caller's ``refused`` list and the scan carries on,
because an empty channel and a refused channel are opposite claims about the
world and only one of them is ours to make. A missing ``aws`` extra should not
cost the whole scan any more than a missing IAM permission does.
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

#: AWS's own legal names, in case `LEGAL_ENTITY_NAME` ever names AWS itself as
#: the seller of record on a Marketplace-billed line (an AWS-native offering
#: sold through the Marketplace listing mechanism, for instance). Skipped for
#: the same reason as `_MARKETPLACE`: reporting AWS as a Marketplace vendor
#: would create a second, phantom "aws" row next to the one the tfstate
#: channel already produces under that exact key.
#:
#: NOT VERIFIED against a real Cost Explorer response — nobody on this project
#: has seen `LEGAL_ENTITY_NAME` actually return one of these on a Marketplace
#: line, so this list may turn out to be unneeded, wrong, or incomplete. Kept
#: as an explicit, narrow list rather than a broad heuristic (e.g. matching
#: `/amazon/i`) on purpose: a heuristic would also swallow a legitimately
#: named seller such as "Amazon Analytics Ltd", which is worse than the
#: phantom row it would prevent. A field test against a real payer account
#: must settle whether this list belongs here at all.
_AWS_SELLING_ENTITIES = frozenset({"amazon web services, inc.", "amazon web services emea sarl"})

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

    Client construction happens inside the same guard as the AWS call itself:
    a mistyped profile in the ``aws:`` block of the sources config raises
    ``ProfileNotFound`` from ``boto3`` before any request is even attempted,
    and that is a typo, not a reason to hand back no register at all.
    """
    try:
        client = client if client is not None else _ce_client(profile)
        start, end = annual_window(today)
        results = _all_results(
            client,
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=[_METRIC],
            Filter={"Dimensions": {"Key": "BILLING_ENTITY", "Values": [_MARKETPLACE]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "LEGAL_ENTITY_NAME"}],
        )
        totals, currencies = _totals_by_seller(results)
    except ClickopsError as e:
        refused.append(f"marketplace: {e}")
        return [], []
    except AwsError as e:
        refused.append(f"marketplace: {e}")
        return [], []
    except Exception as e:  # noqa: BLE001 - any other AWS/client failure degrades this channel too
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


def _all_results(client: CostExplorerClient, **kwargs: Any) -> list[dict[str, Any]]:
    """Every page of ``get_cost_and_usage``, concatenated.

    Cost Explorer paginates a single grouped request over ``NextPageToken``
    once the result set is large enough. A silently truncated page makes a
    real seller vanish exactly the way a nonexistent one would — indistin-
    guishable from the outside, which is the one failure this channel exists
    to rule out, since it is the only channel that ever gets to say FILLED.
    """
    results: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        page = readonly(client, "get_cost_and_usage", **kwargs, **({"NextPageToken": token} if token else {}))
        results.extend(page.get("ResultsByTime", []))
        token = page.get("NextPageToken")
        if not token:
            return results


def _totals_by_seller(results: list[dict[str, Any]]) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Sum each seller's monthly lines.

    Currency is tracked per seller, not globally: two Marketplace sellers
    billing in different currencies must never be added together under one
    shared label. A single seller whose own lines mix currencies mid-window is
    worse, not better — that is a real amount that cannot be summed without
    inventing an exchange rate, so it raises exactly as
    :func:`.aws.collect_annual_expense` does for the identical reason, rather
    than silently keeping whichever currency happened to arrive first.
    """
    totals: dict[str, Decimal] = {}
    currencies: dict[str, str] = {}
    for window in results:
        for group in window.get("Groups", []):
            keys = group.get("Keys") or []
            legal_name = keys[0] if keys else ""
            if not legal_name or legal_name == _MARKETPLACE or legal_name.casefold() in _AWS_SELLING_ENTITIES:
                # No legal entity attributed, the billing-entity placeholder
                # itself, or (unverified — see the constant's docstring) AWS
                # named as its own Marketplace seller: none of these is a
                # third party, and reporting one would invent a legal name
                # nobody billed under, or duplicate the `aws` row under a
                # second, different key.
                continue
            metric = group.get("Metrics", {}).get(_METRIC, {})
            unit = metric.get("Unit")
            if unit:
                previous = currencies.get(legal_name)
                if previous is not None and unit != previous:
                    raise AwsError(
                        f"Cost Explorer returned more than one currency for {legal_name!r} "
                        f"({previous} and {unit}). Adding them together would invent an exchange "
                        f"rate; re-run scoped to a single billing currency."
                    )
                currencies[legal_name] = unit
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
