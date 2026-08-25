"""Hostname to vendor, and the DNS evidence Terraform's provider list cannot show."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dora_roi.collectors.domains import hosts_in, vendor_domains, vendor_for_host
from dora_roi.collectors.tfstate import parse_state_file
from dora_roi.enrichment.mapping import load_mapping


class TestVendorForHost:
    @pytest.mark.parametrize(
        ("host", "vendor"),
        [
            # Every one of these was found in a real Route53 zone.
            ("aspmx.l.google.com", "google"),
            ("_spf.google.com", "google"),
            ("ghs.googlehosted.com", "google"),
            ("7899656.group6.sites.hubspot.net", "hubspot"),
            ("spf06.hubspotemail.net", "hubspot"),
            ("dkim2.mcsv.net", "mailchimp"),
            ("cdn.webflow.com", "webflow"),
            ("ext.teamtailor.com", "teamtailor"),
            ("custom.lemlist.com", "lemlist"),
            ("timely-croquembouche-7022b9.netlify.app", "netlify"),
            ("imaps.aruba.it", "aruba"),
            ("d111.cloudfront.net", "aws"),
        ],
    )
    def test_real_hosts_resolve(self, host: str, vendor: str) -> None:
        assert vendor_for_host(host) == vendor

    @pytest.mark.parametrize(
        "host",
        [
            "evil-hubspot.net",  # a substring is not a suffix
            "hubspot.net.attacker.example",  # nor a prefix of somebody else's zone
            "notgoogle.com",
            "internal.acme.example",
            "",
        ],
    )
    def test_nothing_is_invented(self, host: str) -> None:
        assert vendor_for_host(host) is None

    def test_matching_ignores_case_and_a_trailing_dot(self) -> None:
        assert vendor_for_host("ASPMX.L.GOOGLE.COM.") == "google"

    def test_the_longest_matching_domain_wins(self) -> None:
        assert vendor_for_host("x.hubspotemail.net") == "hubspot"

    def test_every_domain_names_a_provider_the_mapping_knows(self) -> None:
        """A domain pointing at a vendor with no mapping entry is half a feature."""
        unmapped = sorted(set(vendor_domains().values()) - set(load_mapping()))
        assert unmapped == []


class TestHostsIn:
    def test_an_spf_record_names_several_vendors_at_once(self) -> None:
        spf = "v=spf1 include:_spf.google.com include:spf06.hubspotemail.net include:amazonses.com ~all"
        assert {vendor_for_host(h) for h in hosts_in(spf)} == {"google", "hubspot", "aws"}

    def test_spf_syntax_is_not_mistaken_for_a_host(self) -> None:
        assert hosts_in("v=spf1 ~all -all a mx ptr") == []

    def test_a_plain_value_is_one_host(self) -> None:
        assert hosts_in("dkim2.mcsv.net") == ["dkim2.mcsv.net"]


class TestDnsRecordsInState:
    """The channel that turned one vendor into nine on a real estate."""

    def state(self, tmp_path: Path, *records: dict) -> Path:
        resources = [
            {
                "mode": "managed",
                "type": "aws_route53_record",
                "name": f"r{i}",
                "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [{"attributes": attributes}],
            }
            for i, attributes in enumerate(records)
        ]
        path = tmp_path / "dns.tfstate"
        path.write_text(json.dumps({"version": 4, "resources": resources}))
        return path

    def test_an_mx_record_names_your_mail_provider(self, tmp_path: Path) -> None:
        found = {p.name for p in parse_state_file(self.state(tmp_path, {"records": ["1 aspmx.l.google.com"]}))}
        assert "google" in found

    def test_an_spf_txt_names_everyone_allowed_to_send_as_you(self, tmp_path: Path) -> None:
        spf = {"records": ["v=spf1 include:_spf.google.com include:spf06.hubspotemail.net ~all"]}
        found = {p.name for p in parse_state_file(self.state(tmp_path, spf))}
        assert {"google", "hubspot"} <= found

    def test_an_alias_target_counts(self, tmp_path: Path) -> None:
        found = {p.name for p in parse_state_file(self.state(tmp_path, {"alias": {"name": "d1.cloudfront.net"}}))}
        assert "aws" in found

    def test_your_own_hostname_is_not_a_vendor(self, tmp_path: Path) -> None:
        """`name` is your zone, not a counterparty, and must never be read."""
        record = {"name": "www.hubspot.net.example.com", "records": ["10.0.0.1"]}
        assert [p.name for p in parse_state_file(self.state(tmp_path, record))] == ["aws"]

    def test_dns_evidence_is_labelled_as_such(self, tmp_path: Path) -> None:
        found = {p.name: p for p in parse_state_file(self.state(tmp_path, {"records": ["cdn.webflow.com"]}))}
        assert found["webflow"].resource_types["dns_record"] == 1
        assert found["webflow"].registry == "dns"

    def test_a_dns_vendor_merges_with_its_terraform_provider(self, tmp_path: Path) -> None:
        """CloudFront in an alias is more AWS, not a second AWS."""
        found = {p.name: p for p in parse_state_file(self.state(tmp_path, {"records": ["d1.cloudfront.net"]}))}
        assert set(found) == {"aws"}
        assert found["aws"].resource_types["dns_record"] == 1

    def test_the_evidence_cites_the_origin(self, tmp_path: Path) -> None:
        uri = "s3://b/env:/production/dns/terraform.tfstate"
        found = {p.name: p for p in parse_state_file(self.state(tmp_path, {"records": ["ext.teamtailor.com"]}), uri)}
        assert found["teamtailor"].source_files == {uri}

    def test_other_dns_providers_are_read_too(self, tmp_path: Path) -> None:
        state = {
            "version": 4,
            "resources": [
                {
                    "mode": "managed",
                    "type": "google_dns_record_set",
                    "name": "x",
                    "provider": 'provider["registry.terraform.io/hashicorp/google"]',
                    "instances": [{"attributes": {"rrdatas": ["dkim2.mcsv.net"]}}],
                },
                {
                    "mode": "managed",
                    "type": "cloudflare_record",
                    "name": "y",
                    "provider": 'provider["registry.terraform.io/cloudflare/cloudflare"]',
                    "instances": [{"attributes": {"content": "ext.teamtailor.com"}}],
                },
            ],
        }
        path = tmp_path / "multi.tfstate"
        path.write_text(json.dumps(state))
        assert {"mailchimp", "teamtailor"} <= {p.name for p in parse_state_file(path)}

    def test_a_record_pointing_nowhere_known_invents_nothing(self, tmp_path: Path) -> None:
        record = {"records": ["internal.acme.example", "10.0.0.1"]}
        assert [p.name for p in parse_state_file(self.state(tmp_path, record))] == ["aws"]
