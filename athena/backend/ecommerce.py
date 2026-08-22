"""Deterministic, read-only ecommerce backend fixture adapter."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from athena.types import JSONValue

from .contracts import (
    BackendErrorCode,
    BackendQuery,
    BackendQueryResult,
    BackendToolDefinition,
)

ORDER_TOOL = "ecommerce.order.query"
PAYMENT_TOOL = "ecommerce.payment.query"
INVENTORY_TOOL = "ecommerce.inventory.query"
EVENT_TOOL = "ecommerce.message.events"
LOG_TOOL = "ecommerce.service.logs"
METRIC_TOOL = "ecommerce.service.metrics"

_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "token",
    "card_number",
    "cvv",
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"password|secret|token|card[_-]?number|cvv)\s*[:=]\s*([^\s,;]+)"
)


def _schema(
    *, properties: dict[str, JSONValue], required: list[str]
) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


ECOMMERCE_READONLY_TOOL_DEFINITIONS: tuple[BackendToolDefinition, ...] = (
    BackendToolDefinition(
        ORDER_TOOL,
        "Query one order's state and fulfillment summary.",
        _schema(
            properties={
                "order_id": {"type": "string", "minLength": 1, "maxLength": 64}
            },
            required=["order_id"],
        ),
    ),
    BackendToolDefinition(
        PAYMENT_TOOL,
        "Query payment status and provider references for one order.",
        _schema(
            properties={
                "order_id": {"type": "string", "minLength": 1, "maxLength": 64}
            },
            required=["order_id"],
        ),
    ),
    BackendToolDefinition(
        INVENTORY_TOOL,
        "Query available inventory for one SKU, optionally by warehouse.",
        _schema(
            properties={
                "sku_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "warehouse_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            required=["sku_id"],
        ),
    ),
    BackendToolDefinition(
        EVENT_TOOL,
        "Query order message and outbox events in chronological order.",
        _schema(
            properties={
                "order_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "event_type": {"type": "string", "minLength": 1, "maxLength": 64},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            required=["order_id"],
        ),
    ),
    BackendToolDefinition(
        LOG_TOOL,
        "Search a service's structured logs by text or trace ID.",
        _schema(
            properties={
                "service": {"type": "string", "minLength": 1, "maxLength": 64},
                "query": {"type": "string", "maxLength": 200},
                "trace_id": {"type": "string", "maxLength": 128},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            required=["service"],
        ),
    ),
    BackendToolDefinition(
        METRIC_TOOL,
        "Query recent service metric points for a named metric.",
        _schema(
            properties={
                "service": {"type": "string", "minLength": 1, "maxLength": 64},
                "metric": {"type": "string", "minLength": 1, "maxLength": 64},
                "window_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            },
            required=["service", "metric"],
        ),
    ),
)


DEFAULT_ECOMMERCE_FIXTURES: dict[str, Any] = {
    "orders": {
        "ord-1001": {
            "order_id": "ord-1001",
            "user_id": "user-7",
            "status": "paid",
            "payment_status": "paid",
            "fulfillment_status": "pending",
            "total_amount": 299.0,
            "currency": "CNY",
        },
        "ord-1002": {
            "order_id": "ord-1002",
            "user_id": "user-8",
            "status": "paid",
            "payment_status": "paid",
            "fulfillment_status": "shipped",
            "total_amount": 99.0,
            "currency": "CNY",
        },
    },
    "payments": {
        "ord-1001": {
            "order_id": "ord-1001",
            "status": "succeeded",
            "provider": "mock-pay",
            "provider_reference": "pay-ref-1001",
            "payment_token": "tok_mock_should_be_redacted",
        },
        "ord-1002": {
            "order_id": "ord-1002",
            "status": "succeeded",
            "provider": "mock-pay",
            "provider_reference": "pay-ref-1002",
            "payment_token": "tok_mock_should_be_redacted",
        },
    },
    "inventory": {
        "sku-100": [
            {
                "sku_id": "sku-100",
                "warehouse_id": "wh-east",
                "available": 0,
                "reserved": 8,
            },
            {
                "sku_id": "sku-100",
                "warehouse_id": "wh-west",
                "available": 12,
                "reserved": 1,
            },
        ],
    },
    "message_events": {
        "ord-1001": [
            {"event_type": "payment.succeeded", "status": "published", "sequence": 1},
            {"event_type": "order.created", "status": "published", "sequence": 2},
            {"event_type": "fulfillment.requested", "status": "failed", "sequence": 3},
        ],
        "ord-1002": [
            {"event_type": "payment.succeeded", "status": "published", "sequence": 1},
            {
                "event_type": "fulfillment.requested",
                "status": "published",
                "sequence": 2,
            },
        ],
    },
    "logs": [
        {
            "timestamp": "2026-08-14T09:00:00Z",
            "service": "order-service",
            "level": "ERROR",
            "trace_id": "trace-1001",
            "message": "outbox publish failed: broker unavailable",
        },
        {
            "timestamp": "2026-08-14T09:00:01Z",
            "service": "payment-service",
            "level": "INFO",
            "trace_id": "trace-1001",
            "message": "payment confirmed for ord-1001",
        },
    ],
    "metrics": {
        "order-service": [
            {"metric": "http_5xx_rate", "value": 0.18, "unit": "ratio", "minute": 1},
            {
                "metric": "outbox_publish_errors",
                "value": 4,
                "unit": "count",
                "minute": 1,
            },
        ],
        "payment-service": [
            {"metric": "http_5xx_rate", "value": 0.0, "unit": "ratio", "minute": 1},
        ],
    },
}


class MockEcommerceAdapter:
    """Provide deterministic ecommerce observations without writes or network calls."""

    scenario_name = "ecommerce.order_diagnosis"

    def __init__(self, fixtures: Mapping[str, Any] | None = None) -> None:
        data = deepcopy(DEFAULT_ECOMMERCE_FIXTURES)
        if fixtures:
            for name, value in fixtures.items():
                data[name] = deepcopy(value)
        self._fixtures = data

    @property
    def tool_definitions(self) -> tuple[BackendToolDefinition, ...]:
        return ECOMMERCE_READONLY_TOOL_DEFINITIONS

    @property
    def tool_specs(self):
        return tuple(item.as_spec() for item in self.tool_definitions)

    def query(self, request: BackendQuery) -> BackendQueryResult:
        definition = next(
            (item for item in self.tool_definitions if item.name == request.tool_name),
            None,
        )
        if definition is None:
            return self._failure(BackendErrorCode.TOOL_NOT_FOUND)
        validation_error = self._validate(definition.input_schema, request.arguments)
        if validation_error is not None:
            return self._failure(validation_error)
        try:
            data, summary = self._dispatch(request.tool_name, request.arguments)
        except KeyError:
            return self._failure(BackendErrorCode.BACKEND_RECORD_NOT_FOUND)
        return BackendQueryResult(True, summary, self._redact(data))

    def _dispatch(
        self, tool_name: str, arguments: Mapping[str, JSONValue]
    ) -> tuple[JSONValue, str]:
        if tool_name == ORDER_TOOL:
            order_id = str(arguments["order_id"])
            return {
                "data_origin": "mock",
                "order": self._record("orders", order_id),
            }, "order query completed"
        if tool_name == PAYMENT_TOOL:
            order_id = str(arguments["order_id"])
            return {
                "data_origin": "mock",
                "payment": self._record("payments", order_id),
            }, "payment query completed"
        if tool_name == INVENTORY_TOOL:
            sku_id = str(arguments["sku_id"])
            rows = self._fixtures["inventory"].get(sku_id)
            if not rows:
                raise KeyError(sku_id)
            warehouse = arguments.get("warehouse_id")
            selected = [
                row for row in rows if not warehouse or row["warehouse_id"] == warehouse
            ]
            if not selected:
                raise KeyError(warehouse)
            return {
                "data_origin": "mock",
                "items": selected,
            }, "inventory query completed"
        if tool_name == EVENT_TOOL:
            order_id = str(arguments["order_id"])
            events = list(self._fixtures["message_events"].get(order_id, ()))
            if not events:
                raise KeyError(order_id)
            event_type = arguments.get("event_type")
            if event_type:
                events = [item for item in events if item["event_type"] == event_type]
            limit = int(arguments.get("limit", 50))
            return {
                "data_origin": "mock",
                "order_id": order_id,
                "events": events[:limit],
            }, "message event query completed"
        if tool_name == LOG_TOOL:
            service = str(arguments["service"])
            query = str(arguments.get("query", "")).lower()
            trace_id = str(arguments.get("trace_id", ""))
            rows = [
                item
                for item in self._fixtures["logs"]
                if item["service"] == service
                and (not query or query in item["message"].lower())
                and (not trace_id or item["trace_id"] == trace_id)
            ]
            limit = int(arguments.get("limit", 50))
            return {
                "data_origin": "mock",
                "service": service,
                "matches": rows[:limit],
                "match_count": len(rows),
            }, "service log query completed"
        if tool_name == METRIC_TOOL:
            service = str(arguments["service"])
            metric = str(arguments["metric"])
            rows = [
                item
                for item in self._fixtures["metrics"].get(service, ())
                if item["metric"] == metric
            ]
            if not rows:
                raise KeyError(metric)
            return {
                "data_origin": "mock",
                "service": service,
                "metric": metric,
                "points": rows,
                "window_minutes": int(arguments.get("window_minutes", 15)),
            }, "service metric query completed"
        raise KeyError(tool_name)

    def _record(self, group: str, key: str) -> Mapping[str, JSONValue]:
        record = self._fixtures[group].get(key)
        if not record:
            raise KeyError(key)
        return record

    @staticmethod
    def _failure(code: BackendErrorCode) -> BackendQueryResult:
        summaries = {
            BackendErrorCode.TOOL_NOT_FOUND: "requested backend tool is unavailable",
            BackendErrorCode.TOOL_ARGUMENT_REQUIRED: "required tool argument is missing",
            BackendErrorCode.TOOL_ARGUMENT_UNKNOWN: "unknown tool argument is not allowed",
            BackendErrorCode.TOOL_ARGUMENT_INVALID: "tool argument has an invalid value",
            BackendErrorCode.BACKEND_RECORD_NOT_FOUND: "requested backend record was not found",
            BackendErrorCode.BACKEND_QUERY_FAILED: "backend query failed",
        }
        return BackendQueryResult(False, summaries[code], error_code=code)

    @classmethod
    def _validate(
        cls, schema: Mapping[str, JSONValue], arguments: Mapping[str, JSONValue]
    ) -> BackendErrorCode | None:
        required = schema.get("required", [])
        if isinstance(required, list) and any(
            name not in arguments for name in required
        ):
            return BackendErrorCode.TOOL_ARGUMENT_REQUIRED
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and isinstance(
            properties, Mapping
        ):
            if set(arguments) - set(properties):
                return BackendErrorCode.TOOL_ARGUMENT_UNKNOWN
        if not isinstance(properties, Mapping):
            return None
        for name, value in arguments.items():
            definition = properties.get(name)
            if not isinstance(definition, Mapping):
                continue
            expected = definition.get("type")
            if expected == "string" and (
                not isinstance(value, str) or not value.strip()
            ):
                return BackendErrorCode.TOOL_ARGUMENT_INVALID
            if expected == "integer" and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                return BackendErrorCode.TOOL_ARGUMENT_INVALID
            if expected == "integer" and (
                "minimum" in definition
                and value < definition["minimum"]
                or "maximum" in definition
                and value > definition["maximum"]
            ):
                return BackendErrorCode.TOOL_ARGUMENT_INVALID
            if (
                isinstance(value, str)
                and "maxLength" in definition
                and len(value) > definition["maxLength"]
            ):
                return BackendErrorCode.TOOL_ARGUMENT_INVALID
        return None

    @classmethod
    def _redact(cls, value: Any) -> JSONValue:
        if isinstance(value, Mapping):
            return {
                str(key): (
                    "[REDACTED]"
                    if any(
                        marker in str(key).lower().replace("-", "_")
                        for marker in _SENSITIVE_KEY_MARKERS
                    )
                    else cls._redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        if isinstance(value, str):
            return _SENSITIVE_TEXT.sub(r"\1=[REDACTED]", value)
        return value
