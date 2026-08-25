"""Kubernetes collector: the vendors a cluster reveals that Terraform does not.

A cluster leaks three kinds of evidence about who you depend on. Where your
images come from — a registry host is a vendor. What you have declared as
outside the cluster — a ``Service`` of type ``ExternalName`` is, literally, a
dependency you wrote down. And which vendor domains your ingress points at.

Everything here is a *hint* in exactly the sense the tfstate collector's output
is: proof that a relationship exists, not proof of who the counterparty legally
is. Output is the same :class:`DiscoveredProvider` shape, so the rest of the
pipeline neither knows nor cares which collector produced a provider.

The hard rule is the one from the tfstate collector, restated: **nothing is
guessed**. An image on ``registry.internal.acme.example`` produces no provider,
because inventing a vendor for a private registry would put a fiction in a
regulatory filing. Likewise an ingress on your own domain: it is your host, not
somebody's service.

The hostname-to-vendor table lives in :mod:`dora_roi.collectors.domains`, shared
with the Terraform collector: an ingress host and a Route53 record are the same
kind of evidence and deserve the same answer.

Read-only by construction — only ``list_*`` calls. Requires the ``k8s`` extra;
the kubernetes client is imported lazily.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from dora_roi.collectors.domains import vendor_for_host
from dora_roi.collectors.tfstate import DiscoveredProvider

__all__ = ["K8sError", "collect_kubernetes", "vendor_for_image"]


class K8sError(Exception):
    """The cluster could not be read."""


# ECR encodes the region in the host, which is the one place a registry tells
# you something a plain vendor name does not.
_ECR_RE = re.compile(r"^\d+\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com$")

#: Registry host -> Terraform provider name. Exact hosts and suffixes both.
_REGISTRY_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("public.ecr.aws", "aws"),
    (".azurecr.io", "azurerm"),
    ("gcr.io", "google"),
    ("-docker.pkg.dev", "google"),
    ("ghcr.io", "github"),
    ("registry.gitlab.com", "gitlab"),
    ("quay.io", "quay"),
    (".jfrog.io", "jfrog"),
    ("docker.io", "docker"),
    ("index.docker.io", "docker"),
)


def vendor_for_image(image: str) -> tuple[str | None, str | None]:
    """``(provider, region)`` for a container image reference.

    Follows Docker's own rule for telling a registry host from a namespace, and
    the rule has two halves that are easy to get half-right. With no slash at
    all the reference is a Docker Hub official image, so ``redis:7`` is Hub and
    the colon is a tag, not a port. With a slash, the first segment is a host
    only if it has a dot, a colon or is ``localhost``, so ``myteam/api`` is Hub
    too, not a registry called ``myteam``.
    """
    parts = image.split("/", 1)
    if len(parts) == 1:
        return "docker", None
    head = parts[0]
    if "." not in head and ":" not in head and head != "localhost":
        return "docker", None

    host = head.split(":", 1)[0].lower()
    ecr = _ECR_RE.match(host)
    if ecr:
        return "aws", ecr.group("region")
    for suffix, provider in _REGISTRY_SUFFIXES:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            return provider, None
    return None, None


def collect_kubernetes(
    *,
    core_v1: Any | None = None,
    networking_v1: Any | None = None,
    context: str | None = None,
    kubeconfig: str | None = None,
) -> list[DiscoveredProvider]:
    """Read one cluster and return the providers it reveals, heaviest first."""
    if core_v1 is None or networking_v1 is None:
        core_v1, networking_v1, context = _clients(context, kubeconfig)
    label = context or "current-context"

    found: dict[str, DiscoveredProvider] = {}

    for item in _list(core_v1, "list_pod_for_all_namespaces", label):
        spec = getattr(item, "spec", None)
        containers = list(getattr(spec, "containers", None) or []) + list(getattr(spec, "init_containers", None) or [])
        for container in containers:
            provider, region = vendor_for_image(getattr(container, "image", "") or "")
            if provider:
                _record(found, provider, "container_image", label, region)

    for item in _list(core_v1, "list_service_for_all_namespaces", label):
        spec = getattr(item, "spec", None)
        if getattr(spec, "type", None) != "ExternalName":
            continue
        provider = vendor_for_host(getattr(spec, "external_name", "") or "")
        if provider:
            _record(found, provider, "service_externalname", label, None)

    for item in _list(networking_v1, "list_ingress_for_all_namespaces", label):
        for rule in getattr(getattr(item, "spec", None), "rules", None) or []:
            provider = vendor_for_host(getattr(rule, "host", "") or "")
            if provider:
                _record(found, provider, "ingress_host", label, None)

    return sorted(found.values(), key=lambda p: (-p.resource_count, p.name))


def _record(found: dict[str, DiscoveredProvider], name: str, kind: str, context: str, region: str | None) -> None:
    provider = found.get(name)
    if provider is None:
        # A cluster has no Terraform registry or namespace behind it; saying so
        # is better than borrowing plausible-looking ones from the tfstate side.
        provider = DiscoveredProvider(name=name, namespace="", registry="k8s", resource_types=Counter())
        provider.source_files.add(f"k8s:{context}")
        found[name] = provider
    provider.resource_count += 1
    provider.resource_types[kind] += 1
    if region:
        provider.regions.add(region)


def _list(client: Any, operation: str, context: str) -> list[Any]:
    try:
        return list(getattr(client, operation)(watch=False).items or [])
    except AttributeError:
        return []
    except Exception as e:
        raise K8sError(
            f"could not read the cluster for context {context!r} via {operation}: {e}. "
            f"Check that the context exists and that the credentials can list this resource."
        ) from e


def _clients(context: str | None, kubeconfig: str | None) -> tuple[Any, Any, str | None]:
    try:
        from kubernetes import client, config
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise K8sError(
            "the Kubernetes collector needs the `k8s` extra: install with `uv tool install 'dora-roi[k8s]'`."
        ) from e
    try:
        config.load_kube_config(config_file=kubeconfig, context=context)
    except Exception as e:
        raise K8sError(f"could not load the kubeconfig (context={context!r}): {e}") from e
    return client.CoreV1Api(), client.NetworkingV1Api(), context
