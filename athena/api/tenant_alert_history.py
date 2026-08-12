"""Tenant-scoped in-memory presentation history for accepted alert records."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Mapping


class TenantAlertHistory:
    """Keep the legacy alert presentation history isolated by tenant.

    Durable alert receipts remain the source of truth when a database is
    configured. This store only backs the lightweight alert-history view used
    by the web console and never falls back to a process-global list.
    """

    def __init__(self, *, max_records_per_tenant: int = 50) -> None:
        if max_records_per_tenant < 1:
            raise ValueError("max_records_per_tenant must be positive")
        self._max_records_per_tenant = max_records_per_tenant
        self._records: dict[str, list[dict[str, object]]] = {}
        self._lock = RLock()

    def record_response(self, tenant_id: str, response: Mapping[str, object]) -> None:
        """Store each normalized alert record returned by an ingest path."""
        tenant_id = self._require_tenant_id(tenant_id)
        batched = response.get("alerts")
        if isinstance(batched, list):
            records = [item for item in batched if isinstance(item, Mapping)]
        else:
            records = [response]

        # insert(0) is used to retain the existing newest-first history API.
        with self._lock:
            history = self._records.setdefault(tenant_id, [])
            for record in reversed(records):
                history.insert(0, deepcopy(dict(record)))
            del history[self._max_records_per_tenant :]

    def list(self, tenant_id: str, *, limit: int = 20) -> list[dict[str, object]]:
        """Return copies of one tenant's newest records only."""
        tenant_id = self._require_tenant_id(tenant_id)
        bounded_limit = max(1, min(limit, self._max_records_per_tenant))
        with self._lock:
            return deepcopy(self._records.get(tenant_id, [])[:bounded_limit])

    @staticmethod
    def _require_tenant_id(tenant_id: str) -> str:
        normalized = tenant_id.strip()
        if not normalized:
            raise ValueError("tenant_id must be non-empty")
        return normalized
