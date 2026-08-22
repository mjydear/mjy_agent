"""Backend scenario adapters exposed to Agent Runtime integrations."""

from .contracts import (
    BackendErrorCode,
    BackendQuery,
    BackendQueryResult,
    BackendScenario,
    BackendToolAdapter,
    BackendToolDefinition,
)
from .ecommerce import (
    EVENT_TOOL,
    INVENTORY_TOOL,
    LOG_TOOL,
    METRIC_TOOL,
    ORDER_TOOL,
    PAYMENT_TOOL,
    MockEcommerceAdapter,
)
from .tools import (
    ECOMMERCE_READONLY_TOOL_DEFINITIONS,
    BackendReadOnlyToolCatalog,
)

__all__ = [
    "BackendErrorCode",
    "BackendQuery",
    "BackendQueryResult",
    "BackendScenario",
    "BackendToolAdapter",
    "BackendToolDefinition",
    "BackendReadOnlyToolCatalog",
    "EVENT_TOOL",
    "ECOMMERCE_READONLY_TOOL_DEFINITIONS",
    "INVENTORY_TOOL",
    "LOG_TOOL",
    "METRIC_TOOL",
    "MockEcommerceAdapter",
    "ORDER_TOOL",
    "PAYMENT_TOOL",
]
