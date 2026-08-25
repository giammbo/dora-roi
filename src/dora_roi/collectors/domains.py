"""Hostname to vendor, for the evidence that hides in DNS and in cluster config.

A DNS zone is a confession. An MX record names who reads your mail, a CNAME
names who serves a subdomain, an SPF include names everyone allowed to send as
you — and none of it appears in a `required_providers` block. Pointed at a real
estate, the Terraform providers came to one; the DNS records in the state file
it had just read named eight more, every one an ICT third-party service provider
in DORA's sense.

Two collectors need this, so the table is packaged data rather than a constant
in either of them: ``data/vendor_domains.yaml``, alongside the provider mapping
and maintained the same way.

The matching rule is the whole safety property. A domain matches only as the
registrable suffix of a host, never as a substring — ``evil-hubspot.net`` is not
HubSpot, and neither is ``hubspot.net.attacker.example``. Getting that wrong
would invent a counterparty out of a hostname somebody else controls.
"""

from __future__ import annotations

import re
from functools import cache
from importlib import resources

import yaml

__all__ = ["VENDOR_DOMAIN_FILE", "hosts_in", "vendor_domains", "vendor_for_host"]

VENDOR_DOMAIN_FILE = "vendor_domains.yaml"

#: A hostname, as it appears inside a DNS record value or an SPF include. Needs
#: at least one dot and a plausible TLD, so `v=spf1` and `~all` are not hosts.
_HOST = re.compile(r"\b(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b", re.IGNORECASE)


@cache
def vendor_domains() -> dict[str, str]:
    """domain -> provider key, from the packaged table."""
    raw = resources.files("dora_roi.data").joinpath(VENDOR_DOMAIN_FILE).read_text(encoding="utf-8")
    return {str(domain).lower(): str(provider) for domain, provider in (yaml.safe_load(raw) or {}).items()}


def vendor_for_host(host: str) -> str | None:
    """The provider a hostname belongs to, or nothing. Never a guess.

    Longest suffix wins, so a table holding both ``google.com`` and a
    hypothetical ``mail.google.com`` resolves to the more specific one.
    """
    host = host.lower().rstrip(".").removesuffix(":443").removesuffix(":80")
    if not host:
        return None
    table = vendor_domains()
    best: tuple[int, str] | None = None
    for domain, provider in table.items():
        if host == domain or host.endswith(f".{domain}"):
            if best is None or len(domain) > best[0]:
                best = (len(domain), provider)
    return best[1] if best else None


def hosts_in(value: str) -> list[str]:
    """Every hostname inside a record value.

    One string can name several vendors. An SPF record is the clearest case —
    ``v=spf1 include:_spf.google.com include:spf06.hubspotemail.net ~all`` is two
    counterparties in one TXT record, and reading only the first would report
    half a relationship.
    """
    return _HOST.findall(value)
