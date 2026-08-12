"""Static capability bundles for governed workflow/tool selection."""

from __future__ import annotations

from dataclasses import dataclass

from athena.agent.policy.contracts import ToolSpecV2
from athena.tools.cloud.k8s.tools import K8S_READONLY_TOOL_SPECS


@dataclass(frozen=True)
class CapabilityBundle:
    """A versioned set of provider capabilities and tool contracts."""

    bundle_id: str
    version: str
    provider: str
    capabilities: tuple[str, ...]
    tool_specs: tuple[ToolSpecV2, ...]
    workflows: tuple[str, ...] = ()

    def supports_all(self, required: set[str] | frozenset[str]) -> bool:
        return required.issubset(set(self.capabilities))


class CapabilityBundleRegistry:
    """In-process registry for static V1 capability bundles."""

    def __init__(self, bundles: tuple[CapabilityBundle, ...] = ()) -> None:
        self._bundles: dict[str, CapabilityBundle] = {}
        for bundle in bundles:
            self.register(bundle)

    def register(self, bundle: CapabilityBundle) -> None:
        if bundle.bundle_id in self._bundles:
            raise ValueError(
                f"capability bundle already registered: {bundle.bundle_id}"
            )
        self._bundles[bundle.bundle_id] = bundle

    def get(self, bundle_id: str) -> CapabilityBundle | None:
        return self._bundles.get(bundle_id)

    def list(self) -> tuple[CapabilityBundle, ...]:
        return tuple(sorted(self._bundles.values(), key=lambda item: item.bundle_id))

    def select_for(
        self, required_capabilities: set[str] | frozenset[str]
    ) -> tuple[CapabilityBundle, ...]:
        return tuple(
            bundle
            for bundle in self.list()
            if bundle.supports_all(required_capabilities)
        )


def kubernetes_readonly_bundle() -> CapabilityBundle:
    """Return the static Kubernetes readonly bundle used by CloudOps workflows."""
    return CapabilityBundle(
        bundle_id="kubernetes-readonly",
        version="1.0.0",
        provider="kubernetes",
        capabilities=(
            "k8s.workload.read",
            "k8s.events.read",
            "k8s.logs.read",
        ),
        tool_specs=K8S_READONLY_TOOL_SPECS,
        workflows=("crashloop", "pod_pending"),
    )


def default_capability_registry() -> CapabilityBundleRegistry:
    return CapabilityBundleRegistry((kubernetes_readonly_bundle(),))
