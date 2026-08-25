"""AWS Organizations collector: the group perimeter, as AWS understands it.

B_01.02 asks which entities fall within the scope of consolidation. AWS cannot
answer that question — an account is a billing and isolation boundary, not a
legal entity, and the mapping between the two lives in a corporate structure
chart, not in an API. What AWS *can* give you is a strong hint about the shape
of the group and a checklist of things to reconcile against the real one.

So this module produces hints, never rows. The account name is AUTO but still a
hint; the OU path is SEMI. Both carry a caveat that travels with them, because
"acme-prod" is not a legal person and a register that says otherwise is worse
than one with a gap.

Golden rule 2 is enforced here rather than promised: :func:`readonly` refuses
any operation that is not a List, Describe, Get or Search before it reaches the
network. Requires the ``aws`` extra; boto3 is imported lazily so the rest of the
tool works without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_ce.client import CostExplorerClient
    from mypy_boto3_organizations.client import OrganizationsClient
else:
    OrganizationsClient = Any
    CostExplorerClient = Any

#: readonly() is deliberately polymorphic: it guards *any* boto3 client by
#: operation name, and pinning it to one service type would mean a second copy
#: of the guard per service — which is how one of them ends up missing.
AwsClient = Any

__all__ = [
    "READ_ONLY_ACTIONS",
    "AwsError",
    "DiscoveredAccount",
    "ExpenseReport",
    "OrganizationInventory",
    "annual_window",
    "collect_annual_expense",
    "collect_organization",
    "expense_for_provider",
    "readonly",
]

#: The IAM actions this tool ever calls. Ship this list; grant nothing else.
#: Every one is a read — see :func:`readonly`, which enforces it in code.
READ_ONLY_ACTIONS: tuple[str, ...] = (
    "organizations:DescribeOrganization",
    "organizations:ListRoots",
    "organizations:ListAccounts",
    "organizations:ListAccountsForParent",
    "organizations:ListOrganizationalUnitsForParent",
    "ce:GetCostAndUsage",
    "ce:GetTags",
    "tag:GetResources",
    "tag:GetTagKeys",
    "resource-explorer-2:Search",
    "resource-explorer-2:ListViews",
    "iam:ListRoles",
    "iam:ListUsers",
)

_READ_PREFIXES = ("list_", "describe_", "get_", "search")

_ACCOUNT_CAVEAT = (
    "an AWS account is not a legal entity: it is a billing and isolation boundary. "
    "Map it to the entity that appears in your scope of consolidation before filing."
)


class AwsError(Exception):
    """An AWS call failed, or was refused for being a write."""


def readonly(client: AwsClient, operation: str, **kwargs: Any) -> Any:
    """Call ``operation`` on ``client``, refusing anything that is not a read.

    A guard rather than a comment. Golden rule 2 says collectors never mutate
    anything, and the cheapest way to keep that true as the code grows is to
    make the mutating call impossible to write by accident.
    """
    if not operation.startswith(_READ_PREFIXES):
        raise AwsError(
            f"refusing to call {operation!r}: dora-roi is read-only and only issues "
            f"List*, Describe*, Get* and Search* operations."
        )
    try:
        return getattr(client, operation)(**kwargs)
    except AttributeError as e:
        raise AwsError(f"unknown AWS operation {operation!r}") from e


@dataclass(frozen=True)
class DiscoveredAccount:
    """One AWS account, and where it sits in the organization."""

    account_id: str
    name: str
    email: str | None
    status: str
    ou_path: tuple[str, ...]

    @property
    def caveat(self) -> str:
        return _ACCOUNT_CAVEAT

    def as_b0102_hint(self) -> dict[str, str]:
        """The B_01.02 fields this account can *suggest*. No LEI: AWS has none."""
        return {"0020": self.name, "0050": " / ".join(self.ou_path)}


@dataclass(frozen=True)
class OrganizationInventory:
    """The organization, flattened to what the register cares about."""

    organization_id: str
    master_account_id: str
    accounts: list[DiscoveredAccount]


def collect_organization(
    *, client: OrganizationsClient | None = None, profile: str | None = None, region: str | None = None
) -> OrganizationInventory:
    """Walk the organization tree. Read-only, and paginated all the way down."""
    client = client if client is not None else _client(profile, region)

    try:
        organization = readonly(client, "describe_organization")["Organization"]
    except AwsError:
        raise
    except Exception as e:
        raise AwsError(
            f"could not describe the AWS organization ({e}). The caller needs "
            f"organizations:DescribeOrganization, and the account must belong to an organization."
        ) from e

    accounts: list[DiscoveredAccount] = []
    for root in _pages(client, "list_roots", "Roots"):
        _walk(client, root["Id"], (root["Name"],), accounts)

    accounts.sort(key=lambda a: (a.ou_path, a.name))
    return OrganizationInventory(
        organization_id=organization["Id"],
        master_account_id=organization["MasterAccountId"],
        accounts=accounts,
    )


def _walk(client: OrganizationsClient, parent_id: str, path: tuple[str, ...], into: list[DiscoveredAccount]) -> None:
    """Depth-first over OUs, collecting the accounts hanging off each one."""
    for account in _pages(client, "list_accounts_for_parent", "Accounts", ParentId=parent_id):
        into.append(
            DiscoveredAccount(
                account_id=account["Id"],
                name=account.get("Name", ""),
                email=account.get("Email"),
                status=account.get("Status", ""),
                ou_path=path,
            )
        )
    for unit in _pages(client, "list_organizational_units_for_parent", "OrganizationalUnits", ParentId=parent_id):
        _walk(client, unit["Id"], (*path, unit["Name"]), into)


def _pages(client: OrganizationsClient, operation: str, key: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Every page of a list call. An organization outgrows one page quietly."""
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        page = readonly(client, operation, **kwargs, **({"NextToken": token} if token else {}))
        items.extend(page.get(key, []))
        token = page.get("NextToken")
        if not token:
            return items


def _client(profile: str | None, region: str | None) -> OrganizationsClient:
    try:
        import boto3
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise AwsError(
            "the AWS collector needs the `aws` extra: install with `uv tool install 'dora-roi[aws]'`."
        ) from e
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("organizations")


# -- Cost Explorer ----------------------------------------------------------

#: Cost Explorer prices AWS itself exactly. It knows a third-party vendor only
#: when that vendor is bought through AWS Marketplace, where the line item is
#: named after the seller. These are the substrings that identify a seller;
#: `aws` is absent on purpose, because for AWS the answer is the whole bill.
_MARKETPLACE_SELLERS: dict[str, tuple[str, ...]] = {
    "datadog": ("datadog",),
    "snowflake": ("snowflake",),
    "mongodbatlas": ("mongodb",),
    "confluent": ("confluent",),
    "elastic": ("elastic cloud", "elasticsearch"),
    "newrelic": ("new relic",),
    "gitlab": ("gitlab",),
    "grafana": ("grafana",),
    "fastly": ("fastly",),
    "sentry": ("sentry", "functional software"),
}

_COST_METRIC = "UnblendedCost"

#: Cost Explorer groups tax under SERVICE as though it were one. It is not, and
#: leaving it in makes the column incoherent: AWS's own figure would be gross
#: while every Marketplace vendor's is net, because a seller's line never carries
#: the tax. On a real bill it was 18% of the total — not a rounding difference.
_NON_SERVICE_LINES = frozenset({"tax"})


@dataclass(frozen=True)
class ExpenseReport:
    """Twelve trailing months of spend, per Cost Explorer service line.

    The number is deterministic, which is why it is allowed to be FILLED. What
    it is *not* is complete: it covers the payer accounts that were scanned and
    nothing else. :attr:`provenance_note` carries that scope so the caveat
    travels with the value instead of living in a README nobody rereads.
    """

    currency: str | None
    by_service: dict[str, Decimal]
    period: tuple[date, date]
    payer_accounts: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum(self.by_service.values(), Decimal("0"))

    @property
    def provenance_note(self) -> str:
        scope = ", ".join(self.payer_accounts) if self.payer_accounts else "the credentials used for the scan"
        start, end = self.period
        return (
            f"AWS Cost Explorer {_COST_METRIC}, {start.isoformat()} to {end.isoformat()}, net of tax, "
            f"covering only the payer account(s) in scope: {scope}. Spend outside them, and any "
            f"contract billed elsewhere, is not included."
        )


def calling_account(profile: str | None = None) -> str | None:
    """Which AWS account the credentials belong to. Needs no IAM permission.

    Cost Explorer answers for *the account you call it from*. Asked from a member
    account it returns that member's spend, not the organisation's — a plausible
    small number where a large one belongs. The provenance note has to name the
    account that was actually billed, and this is the only way to know it.
    """
    try:
        import boto3

        return str(boto3.Session(profile_name=profile).client("sts").get_caller_identity()["Account"])
    except Exception:  # noqa: BLE001 - an unknown caller is a warning, not a failure
        return None


def annual_window(today: date | None = None) -> tuple[date, date]:
    """Trailing twelve whole months, ending at the start of the current one.

    Whole months, ending at the start of the current one: a partial month would
    make an "annual" figure quietly smaller than a year. Shared by every caller
    that asks Cost Explorer for a year of spend, so this definition of "a year"
    lives in exactly one place instead of drifting between channels.
    """
    end = (today or date.today()).replace(day=1)
    start = end.replace(year=end.year - 1)
    return start, end


def collect_annual_expense(
    *,
    client: CostExplorerClient | None = None,
    today: date | None = None,
    profile: str | None = None,
    payer_accounts: list[str] | None = None,
) -> ExpenseReport:
    """Trailing twelve whole months of spend, grouped by service.

    Whole months, ending at the start of the current one: a partial month would
    make the register's "annual expense" quietly smaller than a year.
    """
    client = client if client is not None else _ce_client(profile)
    start, end = annual_window(today)

    response = readonly(
        client,
        "get_cost_and_usage",
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=[_COST_METRIC],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    totals: dict[str, Decimal] = {}
    currency: str | None = None
    for window in response.get("ResultsByTime", []):
        for group in window.get("Groups", []):
            metric = group.get("Metrics", {}).get(_COST_METRIC, {})
            unit = metric.get("Unit")
            if unit:
                if currency is not None and unit != currency:
                    raise AwsError(
                        f"Cost Explorer returned more than one currency ({currency} and {unit}). "
                        f"Adding them together would invent an exchange rate; re-run scoped to a "
                        f"single billing currency."
                    )
                currency = unit
            service = (group.get("Keys") or [""])[0]
            if service.strip().lower() in _NON_SERVICE_LINES:
                continue
            totals[service] = totals.get(service, Decimal("0")) + _amount(metric.get("Amount"))

    return ExpenseReport(
        currency=currency,
        by_service=totals,
        period=(start, end),
        payer_accounts=payer_accounts or [],
    )


def expense_for_provider(provider_name: str, report: ExpenseReport) -> Decimal | None:
    """What this provider cost, or ``None`` when AWS cannot say.

    ``None`` and ``Decimal("0")`` mean different things and the distinction
    matters: nothing means "AWS cannot price this vendor", zero would mean "this
    vendor is free". Reporting a missing number as zero is how a register ends
    up asserting that a critical provider costs nothing.
    """
    if provider_name == "aws":
        return report.total
    needles = _MARKETPLACE_SELLERS.get(provider_name)
    if not needles:
        return None
    matched = [
        amount for service, amount in report.by_service.items() if any(needle in service.lower() for needle in needles)
    ]
    return sum(matched, Decimal("0")) if matched else None


def _amount(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw or "0"))
    except (InvalidOperation, ValueError) as e:
        raise AwsError(f"Cost Explorer returned an unparseable amount: {raw!r}") from e


def _ce_client(profile: str | None) -> CostExplorerClient:
    try:
        import boto3
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise AwsError(
            "the AWS collector needs the `aws` extra: install with `uv tool install 'dora-roi[aws]'`."
        ) from e
    # Cost Explorer is a global service with a us-east-1 endpoint.
    return boto3.Session(profile_name=profile).client("ce", region_name="us-east-1")
