"""Kubernetes collector. Fake client objects: no cluster, no kubeconfig."""

from __future__ import annotations

from types import SimpleNamespace as Fake

import pytest

from dora_roi.collectors.k8s import (
    K8sError,
    collect_kubernetes,
    vendor_for_host,
    vendor_for_image,
)


def pod(*images: str) -> Fake:
    return Fake(spec=Fake(containers=[Fake(image=i) for i in images], init_containers=None))


def service(name: str, external: str | None) -> Fake:
    return Fake(
        metadata=Fake(name=name), spec=Fake(type="ExternalName" if external else "ClusterIP", external_name=external)
    )


def ingress(*hosts: str) -> Fake:
    return Fake(spec=Fake(rules=[Fake(host=h) for h in hosts]))


class FakeCore:
    def __init__(self, pods=(), services=()) -> None:
        self._pods, self._services = list(pods), list(services)

    def list_pod_for_all_namespaces(self, **_: object) -> Fake:
        return Fake(items=self._pods)

    def list_service_for_all_namespaces(self, **_: object) -> Fake:
        return Fake(items=self._services)


class FakeNetworking:
    def __init__(self, ingresses=()) -> None:
        self._ingresses = list(ingresses)

    def list_ingress_for_all_namespaces(self, **_: object) -> Fake:
        return Fake(items=self._ingresses)


def collect(pods=(), services=(), ingresses=(), context="prod"):
    return {
        p.name: p
        for p in collect_kubernetes(
            core_v1=FakeCore(pods, services), networking_v1=FakeNetworking(ingresses), context=context
        )
    }


class TestRegistryHostToVendor:
    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            ("123456789012.dkr.ecr.eu-south-1.amazonaws.com/api:1.2", "aws"),
            ("public.ecr.aws/nginx/nginx:latest", "aws"),
            ("gcr.io/acme/api:1.0", "google"),
            ("europe-docker.pkg.dev/acme/repo/api:1.0", "google"),
            ("acmecorp.azurecr.io/api:1.0", "azurerm"),
            ("ghcr.io/acme/api:sha-abc", "github"),
            ("registry.gitlab.com/acme/api:1.0", "gitlab"),
            ("quay.io/prometheus/node-exporter:v1.8", "quay"),
            ("acme.jfrog.io/docker/api:1.0", "jfrog"),
            ("docker.io/library/redis:7", "docker"),
            ("redis:7", "docker"),
            ("nginx", "docker"),
        ],
    )
    def test_known_registries(self, image: str, expected: str) -> None:
        assert vendor_for_image(image) == (expected, None) or vendor_for_image(image)[0] == expected

    def test_ecr_yields_its_region(self) -> None:
        assert vendor_for_image("123456789012.dkr.ecr.eu-south-1.amazonaws.com/api:1.2") == ("aws", "eu-south-1")

    def test_an_unknown_private_registry_is_not_guessed(self) -> None:
        assert vendor_for_image("registry.internal.acme.example/api:1.0") == (None, None)

    def test_a_bare_image_is_docker_hub_not_a_local_registry(self) -> None:
        # A host is only a host if it has a dot or a port, per Docker's own rule.
        assert vendor_for_image("myteam/api:1.0") == ("docker", None)


class TestExternalNameToVendor:
    def test_a_known_vendor_domain_resolves(self) -> None:
        assert vendor_for_host("api.datadoghq.eu") == "datadog"

    def test_matching_is_on_the_domain_not_a_substring(self) -> None:
        assert vendor_for_host("datadoghq.eu.evil.example") is None

    def test_an_own_domain_is_not_a_vendor(self) -> None:
        assert vendor_for_host("internal.acme.example") is None


class TestCollect:
    def test_images_become_providers(self) -> None:
        found = collect(pods=[pod("ghcr.io/acme/api:1"), pod("quay.io/x/y:2")])
        assert set(found) == {"github", "quay"}

    def test_containers_are_counted(self) -> None:
        found = collect(pods=[pod("ghcr.io/a/b:1", "ghcr.io/c/d:2"), pod("ghcr.io/e/f:3")])
        assert found["github"].resource_count == 3
        assert found["github"].resource_types["container_image"] == 3

    def test_init_containers_count_too(self) -> None:
        p = pod("ghcr.io/a/b:1")
        p.spec.init_containers = [Fake(image="quay.io/x/y:1")]
        assert set(collect(pods=[p])) == {"github", "quay"}

    def test_ecr_region_reaches_the_provider(self) -> None:
        found = collect(pods=[pod("1234.dkr.ecr.eu-west-3.amazonaws.com/api:1")])
        assert found["aws"].regions == {"eu-west-3"}

    def test_external_name_services_are_dependencies(self) -> None:
        found = collect(services=[service("metrics", "api.datadoghq.eu")])
        assert found["datadog"].resource_types["service_externalname"] == 1

    def test_cluster_ip_services_are_ignored(self) -> None:
        assert collect(services=[service("api", None)]) == {}

    def test_ingress_hosts_on_a_vendor_domain_count(self) -> None:
        found = collect(ingresses=[ingress("cdn.fastly.net")])
        assert found["fastly"].resource_types["ingress_host"] == 1

    def test_ingress_hosts_on_your_own_domain_invent_nothing(self) -> None:
        assert collect(ingresses=[ingress("app.acme.example")]) == {}

    def test_an_unknown_registry_invents_no_provider(self) -> None:
        assert collect(pods=[pod("registry.internal.acme.example/api:1")]) == {}

    def test_the_source_names_the_context(self) -> None:
        found = collect(pods=[pod("ghcr.io/a/b:1")], context="staging")
        assert found["github"].source_files == {"k8s:staging"}

    def test_evidence_from_several_kinds_merges_into_one_provider(self) -> None:
        found = collect(
            pods=[pod("1234.dkr.ecr.eu-south-1.amazonaws.com/api:1")],
            services=[service("s3", "s3.eu-south-1.amazonaws.com")],
        )
        assert found["aws"].resource_count == 2
        assert set(found["aws"].resource_types) == {"container_image", "service_externalname"}

    def test_results_are_sorted_by_weight(self) -> None:
        providers = collect_kubernetes(
            core_v1=FakeCore([pod("ghcr.io/a/b:1"), pod("ghcr.io/c/d:1"), pod("quay.io/x/y:1")]),
            networking_v1=FakeNetworking(),
            context="prod",
        )
        assert [p.name for p in providers] == ["github", "quay"]

    def test_an_empty_cluster_discovers_nothing(self) -> None:
        assert collect_kubernetes(core_v1=FakeCore(), networking_v1=FakeNetworking(), context="prod") == []


class TestErrors:
    def test_an_api_failure_is_actionable(self) -> None:
        class Broken:
            def list_pod_for_all_namespaces(self, **_: object) -> None:
                raise RuntimeError("connection refused")

        with pytest.raises(K8sError, match="connection refused"):
            collect_kubernetes(core_v1=Broken(), networking_v1=FakeNetworking(), context="prod")

    def test_the_error_names_the_context(self) -> None:
        class Broken:
            def list_pod_for_all_namespaces(self, **_: object) -> None:
                raise RuntimeError("boom")

        with pytest.raises(K8sError, match="staging"):
            collect_kubernetes(core_v1=Broken(), networking_v1=FakeNetworking(), context="staging")
