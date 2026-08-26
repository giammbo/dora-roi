"""Vendors that leave a footprint in an AWS account but no trace in your code.

Terraform state answers "what did you declare". This module answers a
different question — "who else is in here" — and the two disagree more often
than anyone expects: on the estate this tool was first proved against, nine of
the ten vendors found appeared in no ``required_providers`` block at all.

Four channels live here, and only one of them is authoritative. AWS
Marketplace charges carry the seller's legal name from a billing API, which is
a fact — the same class of source as the expense figure in :mod:`.aws`.
Identity providers, EventBridge partner sources and cross-account trust
policies (the other three channels in this module) carry a hostname or an
account number, which is a hypothesis about who you contracted with — and this
module never upgrades a hypothesis into a fact. A trusted account this tool
cannot name does not become a vendor with an invented name; it becomes a
declared unknown, which is worth more to an auditor than a guess dressed up as
data.

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

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib import resources
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import yaml

from dora_roi.collectors.aws import AwsError, annual_window, cost_and_usage_pages, readonly
from dora_roi.collectors.domains import vendor_for_host
from dora_roi.collectors.tfstate import DiscoveredProvider
from dora_roi.naming import vendor_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_ce.client import CostExplorerClient
else:
    CostExplorerClient = Any

__all__ = [
    "ANY_ACCOUNT",
    "ClickopsError",
    "ExternalPrincipal",
    "VendorFact",
    "aws_regions",
    "collect_clickops",
    "collect_identity_providers",
    "collect_marketplace",
    "collect_partner_event_sources",
    "collect_trust_relationships",
]

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

    Facts come back keyed by :func:`.naming.vendor_key`, one per key at most:
    see :func:`_sellers_by_key` and :func:`_ambiguous_seller` for what happens
    when two billed names reduce to the same one.
    """
    try:
        client = client if client is not None else _ce_client(profile)
        start, end = annual_window(today)
        results = cost_and_usage_pages(
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
    for key, names in sorted(_sellers_by_key(totals).items()):
        providers.append(
            DiscoveredProvider(
                name=key,
                namespace="aws-marketplace",
                registry="aws-marketplace",
                resource_count=len(names),
                resource_types=Counter({"marketplace_subscription": len(names)}),
                source_files={"aws:ce"},
            )
        )
        if len(names) > 1:
            refused.append(_ambiguous_seller(key, names, totals, currencies))
            continue
        legal_name = names[0]
        facts.append(
            VendorFact(
                key=key,
                legal_name=legal_name,
                annual_spend=totals[legal_name],
                currency=currencies.get(legal_name),
                source="aws:ce",
            )
        )
    return providers, facts


def _sellers_by_key(totals: dict[str, Decimal]) -> dict[str, list[str]]:
    """The seller names on the bill, grouped by the key they will be merged under.

    :func:`_totals_by_seller` groups by the raw ``LEGAL_ENTITY_NAME``;
    :func:`.naming.vendor_key` then folds spelling and legal form away, so two
    billed names can arrive here and leave as one key — ``'Datadog, Inc.'`` and
    ``'Datadog International Ltd'`` both reduce to ``datadog``, and so do
    ``'Confluent Inc'`` and ``'Confluent International Ltd'``. It does not take
    two legal entities: one seller respelled halfway through the twelve-month
    window is enough.

    Grouping happens *here*, before a fact exists, because everything
    downstream of a fact is keyed by exactly this key — ``merge_providers``
    folds the rows together and ``cli._apply_vendor_facts`` looks a fact up by
    it. Two facts under one key mean one of them is dropped, silently, and the
    spend that survives is a fraction of the bill wearing this tool's strongest
    label.
    """
    grouped: dict[str, list[str]] = {}
    for legal_name in sorted(totals):
        grouped.setdefault(vendor_key(legal_name), []).append(legal_name)
    return grouped


#: What a refusal says when the Marketplace call answered and the answer named
#: more than one seller for one key. Its own prefix, not the plain
#: ``marketplace: `` one: nothing failed here, so the surfaces that group
#: refusals by what they mean (`cli._print_refusals`, `methodology.refusal_kind`)
#: must not file it under a billing call that could not be made.
_AMBIGUOUS_SELLER_PREFIX = "marketplace: ambiguous seller"


def _ambiguous_seller(key: str, names: list[str], totals: dict[str, Decimal], currencies: dict[str, str]) -> str:
    """Refuse the fact for one key that more than one billed name reduces to.

    Refused rather than resolved, on the precedent :func:`_totals_by_seller`
    already sets one ambiguity earlier: a seller whose own lines mix currencies
    raises instead of picking one, because the pick would be invented. The same
    reasoning, one step later — summing these names would assert the two are one
    legal entity, and keeping one would report part of a bill as the whole of
    it. Both are answers this channel has no source for.

    Narrower than that precedent on purpose: the currency clash makes the whole
    response unusable, while this one is confined to a single key. Every other
    seller in the same response is unaffected and still gets its fact, so the
    refusal is scoped to the key rather than to the channel.

    The provider row itself survives — these sellers *are* on the bill, and
    dropping the row would hide a counterparty the tool saw. What it loses is
    the FILLED legal name and the FILLED spend, which is exactly the part that
    would have been a guess.
    """
    billed = ", ".join(f"{name!r} ({_money(totals[name], currencies.get(name))})" for name in names)
    return (
        f"{_AMBIGUOUS_SELLER_PREFIX}: {len(names)} names on the Marketplace bill reduce to the vendor key "
        f"{key!r} — {billed}. This channel put no legal name and no annual expense on that row: adding the "
        f"amounts would assert these names are one legal entity, and keeping one would report part of the "
        f"bill as the whole of it. Whatever the row does show for those two fields came from elsewhere — "
        f"the provider mapping, which is a table of guesses. Say which counterparty these names are in the "
        f"overlay (`vendors.yaml`), where a human assertion is FILLED and this channel's guess never "
        f"could be."
    )


def _money(amount: Decimal, currency: str | None) -> str:
    return f"{amount} {currency}" if currency else str(amount)


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


#: Hostnames inside a SAML metadata document or an OIDC issuer URL. Deliberately
#: a regex, never an XML parser: the metadata document comes from outside the
#: tool, and parsing untrusted XML opens exactly the XXE surface this repo's
#: own SECURITY.md names as a vulnerability class. The DNS channel in
#: :mod:`.tfstate` mines hostnames out of Terraform state the same way, for the
#: same reason — a hostname is all either channel ever needs.
_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")

#: The real IAM action name for every operation this channel calls, spelled out
#: rather than derived from the operation name. `list_saml_providers` title-cases
#: cleanly into `ListSAMLProviders`, but a transformation clever enough to get
#: `SAML` and `OpenID` capitalised right by rule is not obviously more
#: trustworthy than a four-entry table that is simply correct by inspection —
#: and the name here is what a user pastes into their own IAM policy, so wrong
#: is not an option.
_ACTION_NAME: dict[str, str] = {
    "list_saml_providers": "ListSAMLProviders",
    "get_saml_provider": "GetSAMLProvider",
    "list_open_id_connect_providers": "ListOpenIDConnectProviders",
    "get_open_id_connect_provider": "GetOpenIDConnectProvider",
}


def collect_identity_providers(client: Any, *, account_id: str, refused: list[str]) -> list[DiscoveredProvider]:
    """Federated identity providers: who authenticates into this account.

    INFERRED, always — unlike :func:`collect_marketplace`, this channel never
    reaches FILLED. A SAML metadata document or an OIDC issuer URL names a
    hostname, and a hostname says who signs your people in; it does not say
    which legal entity you signed a contract with.

    Takes a client, not a profile: the CLI wiring constructs one session per
    account and passes the IAM client straight in, the same convention
    :func:`collect_marketplace` uses for Cost Explorer — nothing here decides
    which account it is looking at beyond the label ``account_id`` gives it.

    Every denial — on either ``list_*`` call, or on ``get_*`` for one provider
    among several — is appended to ``refused`` and the channel carries on
    rather than raising, so one unreadable provider never costs the others.
    """
    found: dict[str, DiscoveredProvider] = {}

    for arn in _listed(client, "list_saml_providers", "SAMLProviderList", refused, account_id):
        try:
            document = readonly(client, "get_saml_provider", SAMLProviderArn=arn).get("SAMLMetadataDocument")
        except Exception as e:  # noqa: BLE001 - one unreadable provider is not a failed scan
            refused.append(_denial(account_id, "get_saml_provider", arn, e))
            continue
        _record(found, document or "", "saml_provider", account_id)

    for arn in _listed(client, "list_open_id_connect_providers", "OpenIDConnectProviderList", refused, account_id):
        try:
            url = readonly(client, "get_open_id_connect_provider", OpenIDConnectProviderArn=arn).get("Url")
        except Exception as e:  # noqa: BLE001 - same as above, for the OIDC side
            refused.append(_denial(account_id, "get_open_id_connect_provider", arn, e))
            continue
        # AWS returns the issuer URL with or without a scheme depending on how
        # it was registered; strip whichever it has (str.removeprefix, not
        # lstrip — lstrip would strip characters, not a prefix, and mangle a
        # bare host that happens to start with an 'h') and add back the one
        # the regex expects, rather than assume either form.
        host = (url or "").removeprefix("https://").removeprefix("http://")
        _record(found, f"https://{host}", "oidc_provider", account_id)

    return sorted(found.values(), key=lambda p: p.name)


def _listed(client: Any, operation: str, key: str, refused: list[str], account_id: str) -> list[str]:
    """Every provider ARN from one ``list_*`` call, or none if it was denied."""
    try:
        response = readonly(client, operation)
    except Exception as e:  # noqa: BLE001 - a denial is a perimeter fact, not a crash
        refused.append(f"{account_id} iam-idp: iam:{_ACTION_NAME[operation]} denied ({type(e).__name__}: {e})")
        return []
    return [entry["Arn"] for entry in response.get(key, []) if entry.get("Arn")]


def _denial(account_id: str, operation: str, arn: str, error: Exception) -> str:
    return f"{account_id} iam-idp: iam:{_ACTION_NAME[operation]} on {arn} ({type(error).__name__}: {error})"


def _record(into: dict[str, DiscoveredProvider], blob: str, evidence: str, account_id: str) -> None:
    """Credit one document's vendor(s) with one unit of ``evidence``.

    A single SAML metadata document routinely names its own vendor's hostname
    twice — once as ``entityID``, once as the SSO ``Location`` — and that is
    one identity provider, not two. Matching is deduplicated to the *set* of
    vendors the document names before anything is counted, so a document that
    mentions the same vendor five times still counts as one ``saml_provider``,
    the way one AWS API object always should — counting raw regex hits instead
    would make the register's numbers a function of how a vendor happened to
    write their metadata, not of how many providers actually exist.
    """
    vendors = {vendor for host in _HOST.findall(blob) if (vendor := vendor_for_host(host)) is not None}
    for vendor in vendors:
        existing = into.get(vendor)
        if existing is None:
            into[vendor] = DiscoveredProvider(
                name=vendor,
                namespace="aws",
                registry="aws-iam-idp",
                resource_count=1,
                resource_types=Counter({evidence: 1}),
                source_files={f"aws:iam:{account_id}"},
            )
        else:
            existing.resource_types[evidence] += 1
            existing.resource_count += 1


#: AWS's own naming convention for a partner event source, spelled out here
#: because it is a fact about the EventBridge API, not something dora-roi
#: infers: ``aws.partner/<host>/<rest>``. ``default`` and any customer-defined
#: source lack it entirely, and are not partner integrations.
_PARTNER_SOURCE_PREFIX = "aws.partner/"


def _all_event_sources(client: Any) -> list[dict[str, Any]]:
    """Every EventBridge event source, walked to the end of ``list_event_sources``'s pagination.

    This API paginates with ``NextToken``, request and response alike — the
    shape :func:`.aws.cost_and_usage_pages` already reads for Cost Explorer under a
    differently-named field (``NextPageToken``), and a different shape again
    from IAM's ``Marker``/``IsTruncated`` pair that :func:`_all_roles` reads.
    Three APIs, three field names for "there is more": stopping at the first
    page here would make a partner integration vanish exactly because it
    happened to sit on page two, indistinguishable from one that was never
    wired in at all.
    """
    sources: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        page = readonly(client, "list_event_sources", **({"NextToken": token} if token else {}))
        sources.extend(page.get("EventSources", []))
        token = page.get("NextToken")
        if not token:
            return sources


def collect_partner_event_sources(
    client: Any, *, account_id: str, region: str, refused: list[str]
) -> list[DiscoveredProvider]:
    """SaaS partners wired straight into this account's event bus.

    INFERRED, always, for the same reason as :func:`collect_identity_providers`:
    a partner event source name carries the hostname AWS uses to label the
    integration (``aws.partner/datadoghq.com/...``), and a hostname says who
    is pushing events in, not which legal entity you contracted with.

    Regional, unlike every other channel in this module: an EventBridge event
    bus exists per region, so the caller runs this once per account *and* per
    region already in the perimeter — never every enabled region, which would
    multiply the calls by twenty to find nothing almost every time, and the
    methodology exists precisely to say which regions were never looked at.
    ``region`` lands in ``source_files`` as ``aws:events:<account_id>:<region>``
    so a finding here is never confused with one from :func:`collect_identity_providers`,
    which is account-wide.

    Takes a client, not a profile or a session: the caller constructs one
    ``events`` client per region and passes it straight in, the same
    convention every other channel in this module uses.

    Walks ``list_event_sources`` to the end of its own pagination (see
    :func:`_all_event_sources`) before deciding this bus has no partner
    sources at all: an account with enough integrations to fill a page must
    not lose whatever sits on the next one.
    """
    try:
        sources = _all_event_sources(client)
    except Exception as e:  # noqa: BLE001 - a denial is a perimeter fact, not a crash
        refused.append(f"{account_id}/{region} eventbridge: events:ListEventSources denied ({type(e).__name__}: {e})")
        return []

    found: dict[str, DiscoveredProvider] = {}
    for source in sources:
        vendor = _partner_source_vendor(source.get("Name", ""))
        if vendor is None:
            continue
        existing = found.get(vendor)
        if existing is None:
            found[vendor] = DiscoveredProvider(
                name=vendor,
                namespace="aws",
                registry="aws-eventbridge",
                resource_count=1,
                resource_types=Counter({"partner_event_source": 1}),
                regions={region},
                source_files={f"aws:events:{account_id}:{region}"},
            )
        else:
            existing.resource_types["partner_event_source"] += 1
            existing.resource_count += 1

    return sorted(found.values(), key=lambda p: p.name)


def _partner_source_vendor(name: str) -> str | None:
    """The vendor named by one EventBridge event source, or nothing.

    A source name that does not carry the partner prefix at all — ``default``,
    a customer-defined source — is skipped before a host is even extracted:
    it is not a partner integration, so it is not a candidate vendor either.
    An unrecognised host past the prefix is skipped the same way
    :func:`.domains.vendor_for_host` skips any other unmapped host: nothing
    invented, ever.
    """
    if not name.startswith(_PARTNER_SOURCE_PREFIX):
        return None
    host = name[len(_PARTNER_SOURCE_PREFIX) :].split("/", 1)[0]
    return vendor_for_host(host)


#: The packaged account-ID -> vendor-key table's filename, next to
#: ``provider_mapping.yaml`` inside the ``dora_roi.data`` package.
_VENDOR_ACCOUNTS_FILE = "aws_vendor_accounts.yaml"

#: A 12-digit account inside an IAM principal ARN, e.g. the account in
#: ``arn:aws:iam::464622532012:root`` or ``...:role/SomeRole``. IAM accepts
#: several ARN partitions (``aws``, ``aws-cn``, ``aws-us-gov``) ahead of the
#: same ``:iam::<account>:`` shape, hence the partition class rather than a
#: literal ``aws``.
_ACCOUNT_ARN = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):")

#: The other shape IAM accepts as a trust policy principal: a bare account
#: number with no ARN wrapper at all (``"Principal": {"AWS": "464622532012"}``).
_BARE_ACCOUNT = re.compile(r"^\d{12}$")

#: What IAM writes when a trust policy names no account at all:
#: ``"Principal": "*"`` or ``"Principal": {"AWS": "*"}``. Whether every AWS
#: account can in fact assume such a role is a separate question this module
#: does not answer: a ``Condition`` on the same statement (``aws:PrincipalOrgID``
#: is the common one) can narrow it, and this module reads only
#: ``sts:ExternalId`` out of ``Condition`` — so what it records is the
#: principal the policy names, never an effective-access verdict. Carried
#: through this module as an
#: :class:`ExternalPrincipal` whose ``account_id`` is this exact string, rather
#: than as a fourth field on that class: every surface downstream —
#: ``perimeter.unnamed_principals`` in ``roi_prefill.json``,
#: :func:`.preflight.load_unnamed_principals`, the methodology note — already
#: carries an account id from end to end, and one that is not a number is
#: visible in all of them at once. Nothing can mistake it for a real account:
#: no AWS account id contains a ``*``, and every surface that renders one
#: branches on this constant explicitly.
ANY_ACCOUNT = "*"


@dataclass(frozen=True)
class ExternalPrincipal:
    """An outside AWS account trusted to assume a role here, that no table can name.

    Not a failure of this channel — the finding it exists to produce. Golden
    rule 1 forbids turning a hypothesis into a fact; naming a company from an
    account number nobody vouched for would do exactly that, so an account
    absent from :func:`_vendor_accounts` lands here instead of in
    :class:`.tfstate.DiscoveredProvider`. Later tasks read this list to put
    "an external account has standing access and we cannot say whose" in the
    methodology note and raise it as a finding — a true, actionable statement
    that a guessed vendor name never could have been.

    ``account_id`` is :data:`ANY_ACCOUNT` — the literal ``*`` — when the trust
    policy restricts the principal to no account at all. That is not a lesser
    version of this finding but a wider one, and it used to produce nothing
    whatsoever: ``*`` matches neither account regex, so the role vanished and
    the methodology note rendered *Cross-account trust: read, no result* over
    the broadest principal a trust policy can name (finding C2).
    """

    account_id: str
    role_name: str
    has_external_id: bool


def _vendor_accounts() -> dict[str, str]:
    """The packaged account-ID -> vendor-key table.

    Deliberately incomplete — see the file's own header for why, and for what
    it takes to add a row. Every value here must be a key
    :func:`.mapping.load_mapping` recognises; :data:`_VENDOR_ACCOUNTS_FILE`'s
    own test enforces that, the same way the domain table's coverage is
    enforced for the DNS-based channels.
    """
    raw = resources.files("dora_roi.data").joinpath(_VENDOR_ACCOUNTS_FILE).read_text(encoding="utf-8")
    return {str(k): str(v) for k, v in (yaml.safe_load(raw) or {}).items()}


def _trust_policy(document: Any) -> dict[str, Any] | None:
    """Parse one role's ``AssumeRolePolicyDocument``, or say it could not be read.

    Verified against a real ``boto3`` IAM client (moto included — it goes
    through the same botocore response parsing a live call would): botocore
    auto-decodes this field into an already-parsed ``dict`` before this
    module ever sees it, via its own IAM policy-document handler. A plain
    JSON string only arrives here from something that bypassed that handler —
    this module's own hand-written test doubles standing in for a malformed
    response, or a client wired up in a way this codebase has not exercised —
    so both shapes are accepted rather than one being assumed. Anything that
    is neither a ``dict`` nor ``unquote``-and-``json.loads``-able, including a
    string that survives the decode step but is not valid JSON, returns
    ``None``, which the caller turns into a refusal for that one role, never
    for the scan.
    """
    if isinstance(document, dict):
        return document
    try:
        parsed = json.loads(unquote(document or "{}"))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _statements(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``Allow`` statement in a trust policy; ``Deny`` is excluded.

    A ``Deny`` statement names a principal it locks out, not one it trusts.
    Crediting a vendor, or flagging an unknown, from a statement that exists
    to refuse someone would report the opposite of what the policy says.
    """
    raw = policy.get("Statement", [])
    statements = raw if isinstance(raw, list) else [raw]
    return [s for s in statements if isinstance(s, dict) and s.get("Effect") == "Allow"]


def _trusted_accounts(principal: Any) -> list[str]:
    """Every account one ``Allow`` statement's ``Principal`` trusts.

    Two shapes reach here. The documented one is a mapping, whose ``AWS`` key
    holds the account principals. The other is the bare string ``"*"``, which
    IAM accepts in place of the mapping and which restricts the principal to
    no account at all: the first cut of this function required a mapping and
    returned nothing for it, so a role naming that principal produced no
    vendor, no unknown and no refusal (finding C2). Anything else — a mapping
    with only a ``Service`` or ``Federated`` key, a list, a number — names no
    AWS account and yields nothing, as before.
    """
    if isinstance(principal, str):
        return _principal_accounts(principal)
    if isinstance(principal, dict):
        return _principal_accounts(principal.get("AWS"))
    return []


def _principal_accounts(principal: Any) -> list[str]:
    """Every distinct AWS account named by one statement's ``Principal.AWS``.

    That value arrives as a single string for one trusted principal or a list
    for several; either way, only entries that resolve to a 12-digit account
    (via :data:`_ACCOUNT_ARN` or :data:`_BARE_ACCOUNT`) or to the ``*``
    wildcard survive here — a service principal or a malformed entry is
    silently not an account, the same way :func:`_account_of` treats anything
    else it cannot parse.
    """
    raw = principal if isinstance(principal, list) else [principal]
    accounts = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        account = _account_of(entry)
        if account is not None:
            accounts.append(account)
    return accounts


def _account_of(principal: str) -> str | None:
    """The account inside a principal: an ARN's, a bare one, or all of them.

    ``*`` returns :data:`ANY_ACCOUNT` rather than ``None``. It is the one
    principal that names no account because it restricts to none, and
    dropping it here is what made the broadest principal a trust policy can
    name indistinguishable from a role nobody outside can assume (finding
    C2).

    A principal can also be an AWS service name (``lambda.amazonaws.com``) or
    a federated identity ARN with no account-shaped segment in the position
    this regex checks; both fall through to ``None`` rather than being
    mistaken for a trusted outside account.
    """
    if principal == ANY_ACCOUNT:
        return ANY_ACCOUNT
    if _BARE_ACCOUNT.match(principal):
        return principal
    match = _ACCOUNT_ARN.match(principal)
    return match.group(1) if match else None


def _has_external_id(statement: dict[str, Any]) -> bool:
    """Whether this statement conditions the trust on an ``sts:ExternalId``.

    An external ID is not proof of who the other side is — a vendor and an
    attacker who somehow learned the string would satisfy this identically —
    but its presence is still worth recording: a trust unconditioned on
    anything but the account number is the weaker of the two shapes vendors
    actually use, and later tasks may want to tell them apart.
    """
    condition = statement.get("Condition")
    if not isinstance(condition, dict):
        return False
    string_equals = condition.get("StringEquals")
    if not isinstance(string_equals, dict):
        return False
    return bool(string_equals.get("sts:ExternalId"))


def _all_roles(client: Any) -> list[dict[str, Any]]:
    """Every IAM role, walked to the end of ``list_roles``'s pagination.

    IAM signals more pages with ``IsTruncated`` plus a ``Marker`` to send back
    on the next call — not the ``NextToken`` shape :func:`.aws.cost_and_usage_pages` reads
    for Cost Explorer, or :func:`_all_event_sources` reads for EventBridge,
    and not :func:`.aws._pages`'s shape either, which is typed to the
    Organizations client specifically. IAM's default page size is 100 roles,
    routinely fewer than a real production account holds. Stopping at the
    first page would make this channel's own finding depend on where in
    ``ListRoles``'s ordering a role happened to land: a vendor role on page
    two would produce no provider row, an unnamed external principal on page
    two would produce no declared unknown, and the methodology note that
    reads ``refused`` would call the scan clean — a false negative dressed up
    as a complete one, which is exactly the failure this channel exists to
    rule out.
    """
    roles: list[dict[str, Any]] = []
    marker: str | None = None
    while True:
        page = readonly(client, "list_roles", **({"Marker": marker} if marker else {}))
        roles.extend(page.get("Roles", []))
        marker = page.get("Marker")
        if not page.get("IsTruncated") or not marker:
            return roles


def collect_trust_relationships(
    client: Any, *, account_id: str, own_accounts: frozenset[str], refused: list[str]
) -> tuple[list[DiscoveredProvider], list[ExternalPrincipal]]:
    """Roles an outside AWS account can assume, and who those accounts belong to.

    The most DORA-relevant channel in this module and the easiest to get
    catastrophically wrong: a vendor with standing access to production is
    exactly what a supervisor asks about, and inventing its name to fill a gap
    would put a false counterparty in a regulatory filing. So this function
    returns two lists rather than one — the vendors :func:`_vendor_accounts`
    can name, and, separately, every trusted account it cannot. The second
    list is not a lesser result; it is this channel's own finding, the same
    way an empty channel elsewhere is a fact about the perimeter rather than a
    failure to report one.

    ``own_accounts`` is every account this scan's own perimeter already
    covers — every Organizations member account read via :mod:`.aws`, or just
    ``account_id`` on its own when Organizations was not reachable. A role
    trusted by a sibling account, or by the very account being scanned, is not
    a third party and must not appear as either a vendor or an unknown.

    A role whose principal is ``*`` names no account for that subtraction to
    work on and none for :func:`_vendor_accounts` to look up, so it comes back
    as an :class:`ExternalPrincipal` whose ``account_id`` is
    :data:`ANY_ACCOUNT`.

    Needs only ``iam:ListRoles``: unlike :func:`collect_identity_providers`,
    which pairs a ``List*`` and a ``Get*`` call per provider, ``list_roles``
    already returns each role's ``AssumeRolePolicyDocument`` inline. Walked to
    the end of its own pagination before this function decides the account
    has nothing more to say (see :func:`_all_roles`) — a denial on any page
    costs the whole channel, since there is no narrower call to fall back to,
    and is appended to ``refused`` rather than raised. Past that, one role
    with a trust policy this module cannot parse skips that role alone: a
    single weird policy from years ago is never the reason the rest of the
    account's trust relationships go unreported.
    """
    try:
        roles = _all_roles(client)
    except Exception as e:  # noqa: BLE001 - a denial is a perimeter fact, not a crash
        refused.append(f"{account_id} trust: iam:ListRoles denied ({type(e).__name__}: {e})")
        return [], []

    table = _vendor_accounts()
    found: dict[str, DiscoveredProvider] = {}
    unknown: list[ExternalPrincipal] = []

    for role in roles:
        name = role.get("RoleName", "")
        policy = _trust_policy(role.get("AssumeRolePolicyDocument"))
        if policy is None:
            refused.append(f"{account_id} trust: unreadable trust policy on role {name!r}")
            continue

        for statement in _statements(policy):
            has_external_id = _has_external_id(statement)

            for trusted in _trusted_accounts(statement.get("Principal")):
                if trusted in own_accounts or trusted == account_id:
                    continue
                vendor = table.get(trusted)
                if vendor is None:
                    unknown.append(
                        ExternalPrincipal(account_id=trusted, role_name=name, has_external_id=has_external_id)
                    )
                    continue
                existing = found.get(vendor)
                if existing is None:
                    found[vendor] = DiscoveredProvider(
                        name=vendor,
                        namespace="aws",
                        registry="aws-trust",
                        resource_count=1,
                        resource_types=Counter({"assume_role_trust": 1}),
                        source_files={f"aws:iam:{account_id}"},
                    )
                else:
                    existing.resource_types["assume_role_trust"] += 1
                    existing.resource_count += 1

    return sorted(found.values(), key=lambda p: p.name), unknown


#: Provider names whose ``region`` attribute holds an AWS region. Two, because
#: ``awscc`` (AWS Cloud Control) addresses the same regions as ``aws`` does.
#: Matched on the provider name rather than the registry host so a private
#: mirror (``terraform.example.com/hashicorp/aws``) is still the AWS provider,
#: and so the ``aws`` rows the Kubernetes collector produces for ECR — whose
#: registry is ``k8s`` and whose namespace is empty — are included too, since
#: an ECR hostname carries a real AWS region.
_AWS_PROVIDER_NAMES = frozenset({"aws", "awscc"})


def aws_regions(providers: Iterable[DiscoveredProvider]) -> frozenset[str]:
    """The AWS regions in a set of discovered providers, and nothing else.

    :func:`.tfstate._region_of` reads ``attributes["region"]`` off *any*
    resource of *any* provider, so a state file with DigitalOcean, Scaleway,
    Google or Linode resources in it contributes ``nyc3``, ``fr-par``,
    ``europe-west1`` or ``us-east`` to that pool. Handing those to
    :func:`collect_clickops` built an ``events`` client per string, botocore
    resolved ``events.nyc3.amazonaws.com``, and the connection error came back
    as ``"<acct>/nyc3 eventbridge: events:ListEventSources denied"`` — AWS
    refused nothing; dora-roi asked a question that does not exist. Worse, one
    such string refused the whole EventBridge channel for that account
    (:func:`.methodology._eventbridge_line`), so a clean read in ``eu-west-1``
    was masked by a region AWS never had.

    Filtering by provider is what keeps this a statement about the perimeter
    rather than about string shapes: a region belongs here because an AWS
    resource named it, not because it looks like an AWS region.
    """
    return frozenset(
        region for provider in providers if provider.name in _AWS_PROVIDER_NAMES for region in provider.regions
    )


def collect_clickops(
    *,
    profile: str | None,
    role_arn: str | None,
    account_id: str,
    own_accounts: frozenset[str],
    regions: frozenset[str],
    refused: list[str],
) -> tuple[list[DiscoveredProvider], list[ExternalPrincipal]]:
    """Every per-account channel, for one account.

    Marketplace is deliberately not here: Cost Explorer answers for the payer,
    so it runs once per scan and has its own entry point, :func:`collect_marketplace`.
    Folding it into a per-account loop would ask the same question once per
    account and risk summing one bill N times over.

    A bad profile or an unassumable role costs this one account, not the
    sweep: the caller passes a fresh ``refused`` for every account it visits
    to accumulate into, and a denial here is appended to it and returned as an
    empty result rather than raised, the same contract every channel in this
    module keeps.
    """
    try:
        session = _session(profile=profile, role_arn=role_arn)
    except (AwsError, ClickopsError) as e:
        refused.append(f"{account_id}: no usable credentials ({e})")
        return [], []

    iam = session.client("iam")
    providers = collect_identity_providers(iam, account_id=account_id, refused=refused)
    trusted, unknown = collect_trust_relationships(
        iam, account_id=account_id, own_accounts=own_accounts, refused=refused
    )
    providers.extend(trusted)

    for region in sorted(regions):
        providers.extend(
            collect_partner_event_sources(
                session.client("events", region_name=region),
                account_id=account_id,
                region=region,
                refused=refused,
            )
        )
    return providers, unknown


def _session(*, profile: str | None, role_arn: str | None) -> Any:
    """A boto3 session, optionally after assuming a role.

    ``sts:AssumeRole`` does not go through :func:`.aws.readonly`, and it is
    not itself a read — it is a declared exception to golden rule 2, not a
    silent one. It is defensible rather than swept under the rug: assuming a
    role changes nothing in the target account, and every call made *through*
    the session this returns still goes through ``readonly`` exactly like
    every other credential this tool ever uses. Nothing downstream is allowed
    to reach for ``assume_role`` itself as a shortcut around the guard — this
    function is the one seam in this module where it happens.

    It is not the only place in the codebase that does this: :mod:`.sources`'s
    ``_s3_client`` assumes a role to fetch Terraform state from S3 for the
    identical reason. That module's own docstring now declares it too, on the
    same reasoning as here.
    """
    try:
        import boto3
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise ClickopsError(
            "the AWS collector needs the `aws` extra: install with `uv tool install 'dora-roi[aws]'`."
        ) from e

    try:
        # boto3 validates the profile name against the local config eagerly,
        # from the constructor itself — a typo here (`ProfileNotFound`) never
        # reaches `readonly()` for anything to catch. This module's own
        # header names a mistyped profile as a runtime denial, on the same
        # footing as a throttled call, not a reason for the whole sweep — let
        # alone the whole scan — to abort.
        session = boto3.Session(profile_name=profile)
    except Exception as e:  # noqa: BLE001 - a mistyped profile is a runtime denial, not a crash
        raise AwsError(f"could not create a session for profile {profile!r}: {e}") from e
    if role_arn is None:
        return session
    try:
        assumed = session.client("sts").assume_role(RoleArn=role_arn, RoleSessionName="dora-roi")
    except Exception as e:  # noqa: BLE001 - any assume-role failure costs this account, not the scan
        raise AwsError(f"could not assume {role_arn}: {e}") from e
    credentials = assumed["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )
