"""Turning a company's name into the key the register merges on.

A vendor arrives spelled three ways in one scan: `datadog` from a Terraform
provider, `datadoghq.com` from a DNS record, `"Datadog, Inc."` from a bill.
They are one counterparty and must reach one row, so every channel reduces its
spelling to the same key before anything is merged.

The reduction is deliberately blunt — lowercase, drop punctuation, drop the
legal form — because the alternative is fuzzy matching, and fuzzy matching is
how `Google` quietly absorbs `Google Cloud EMEA Limited`. Under-merging leaves
two rows that are both true; over-merging deletes a counterparty.
"""

from __future__ import annotations

import re

__all__ = ["LEGAL_FORMS", "significant_words", "vendor_key"]

#: Words that say what kind of company it is, not which one.
LEGAL_FORMS: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "llc",
        "lp",
        "llp",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "ag",
        "sa",
        "sas",
        "sarl",
        "srl",
        "spa",
        "bv",
        "nv",
        "ab",
        "as",
        "oy",
        "aps",
        "kft",
        "sp",
        "zoo",
        "pty",
        "group",
        "holding",
        "holdings",
        "international",
    }
)

#: Periods are dropped rather than turned into spaces, so an abbreviated legal
#: form like "N.V." or "S.p.A." collapses to one word ("nv", "spa") instead of
#: splitting into single letters that no longer match `LEGAL_FORMS` at all.
_PERIOD = re.compile(r"\.")
#: Everything else that is not a letter or digit is a separator between words.
_SEPARATOR = re.compile(r"[^a-z0-9]+")


def _normalise(name: str) -> str:
    """Lowercase, legal-form periods collapsed, everything else a space."""
    return _SEPARATOR.sub(" ", _PERIOD.sub("", name.lower())).strip()


def significant_words(name: str) -> set[str]:
    """The words that identify the company, with the legal form removed."""
    return {word for word in _normalise(name).split() if word not in LEGAL_FORMS}


def vendor_key(name: str) -> str:
    """The merge key for a company name.

    Words are joined without a separator so `Sumo Logic` and `SumoLogic` agree,
    which is the difference a billing export and a provider table routinely
    disagree on.
    """
    words = [word for word in _normalise(name).split() if word not in LEGAL_FORMS]
    if not words:
        # A name made only of legal forms is not identifying, but returning ""
        # would make every such vendor collide into one row.
        words = _normalise(name).split()
    return "".join(words)
