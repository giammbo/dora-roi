"""Vendor keys: the same name, however a bill spells it, must reach one key."""

import pytest

from dora_roi.naming import vendor_key


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
