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

#: Narrow on purpose. This is the exact tokenisation GLEIF's same-name and
#: shared-word checks used before this module existed: only `. , - ' '`
#: (straight and curly apostrophe) are separators, and everything else —
#: `&`, `/`, digits glued to letters — stays inside the word. `significant_words`
#: keeps it narrow because widening it changes what `_shares_a_word` accepts:
#: "AT&T" would fracture into "at" and "t", and "t" alone is a coin flip away
#: from matching some other company's name by accident.
_WORD_NOISE = re.compile(r"[.,\-’']")

#: Blunter than `_WORD_NOISE` on purpose. `vendor_key` builds a brand-new merge
#: key, not a comparison inherited from GLEIF, so it can afford — and needs —
#: to fold everything that is not a letter or digit into a single separator.
#: Periods are dropped first rather than turned into spaces, so an abbreviated
#: legal form like "N.V." or "S.p.A." collapses to one word ("nv", "spa")
#: instead of splitting into single letters that no longer match `LEGAL_FORMS`.
_KEY_PERIOD = re.compile(r"\.")
_KEY_SEPARATOR = re.compile(r"[^a-z0-9]+")


def _normalise_for_words(name: str) -> str:
    """Casefold, GLEIF's narrow noise removed, whitespace collapsed.

    This reproduces `gleif._normalise` exactly (same regex, same `casefold`),
    because `significant_words` backs `_shares_a_word`'s fuzzy-candidate
    decision and that decision must keep splitting a name exactly as it did
    before this function existed.
    """
    return " ".join(_WORD_NOISE.sub(" ", name).casefold().split())


def _normalise_for_key(name: str) -> str:
    """Lowercase, legal-form periods collapsed, everything else a space.

    Used only by `vendor_key`, which has no prior behaviour to preserve.
    """
    return _KEY_SEPARATOR.sub(" ", _KEY_PERIOD.sub("", name.lower())).strip()


def significant_words(name: str) -> set[str]:
    """The words that identify the company, with the legal form removed.

    Tokenises narrowly (see `_normalise_for_words`): this is GLEIF's
    same-name/shared-word logic, extracted rather than reinvented, so it must
    keep accepting and rejecting exactly the candidates it always did.
    """
    return {word for word in _normalise_for_words(name).split() if word not in LEGAL_FORMS}


def vendor_key(name: str) -> str:
    """The merge key for a company name.

    Uses the broad normalisation (see `_normalise_for_key`), not the narrow one
    `significant_words` uses: words are joined without a separator so
    `Sumo Logic` and `SumoLogic` agree, and punctuation such as `&` or `/` is
    dropped rather than preserved, which is fine here because a merge key
    answers only to itself — it has no prior comparison to stay faithful to.
    """
    words = [word for word in _normalise_for_key(name).split() if word not in LEGAL_FORMS]
    if not words:
        # A name made only of legal forms is not identifying, but returning ""
        # would make every such vendor collide into one row.
        words = _normalise_for_key(name).split()
    return "".join(words)
