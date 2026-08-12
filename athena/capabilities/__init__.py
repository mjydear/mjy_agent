"""Static capability bundle registry."""

from athena.capabilities.bundles import (
    CapabilityBundle,
    CapabilityBundleRegistry,
    default_capability_registry,
    kubernetes_readonly_bundle,
)

__all__ = [
    "CapabilityBundle",
    "CapabilityBundleRegistry",
    "default_capability_registry",
    "kubernetes_readonly_bundle",
]
