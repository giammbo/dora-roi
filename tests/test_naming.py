"""Vendor keys: the same name, however a bill spells it, must reach one key."""

import pytest

from dora_roi.naming import significant_words, vendor_key


class TestSignificantWords:
    """The narrow tokenisation `gleif._shares_a_word` depends on: punctuation
    beyond `. , - ' '` stays inside a word here, unlike in `vendor_key`."""

    def test_an_ampersand_stays_inside_the_word(self) -> None:
        """ "AT&T" must stay one token — not "at" and "t", which would let it
        share a "significant word" with any name that happens to contain a
        standalone "t"."""
        assert significant_words("AT&T Corp") == {"at&t"}

    def test_an_internal_period_abbreviation_splits_into_single_letters(self) -> None:
        """Unlike `vendor_key`, this does not collapse "N.V." into the legal
        form "nv" — periods are separators here, exactly as they were in
        GLEIF's tokenisation before the extraction."""
        assert significant_words("Acme N.V.") == {"acme", "n", "v"}


class TestVendorKey:
    @pytest.mark.parametrize(
        ("legal_name", "expected"),
        [
            ("Datadog, Inc.", "datadog"),
            ("DATADOG INC", "datadog"),
            ("Snowflake Inc.", "snowflake"),
            ("Elastic N.V.", "elastic"),
            ("MongoDB, Inc.", "mongodb"),
            ("Sumo Logic, Inc.", "sumologic"),
        ],
    )
    def test_a_billing_name_reaches_the_provider_key(self, legal_name: str, expected: str) -> None:
        assert vendor_key(legal_name) == expected

    def test_a_name_that_is_only_a_legal_form_does_not_become_empty(self) -> None:
        """Better a useless key than a key that collides with every other empty one."""
        assert vendor_key("Inc.") == "inc"

    def test_two_different_companies_do_not_collide(self) -> None:
        assert vendor_key("Google LLC") != vendor_key("Google Cloud EMEA Limited")
