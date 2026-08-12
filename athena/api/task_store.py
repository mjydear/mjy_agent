"""
任务 / 指标 / 评测报告持久化存储：把原本进程内的运行状态落到缓存后端（Redis/内存）。

企业级诉求：服务重启不丢任务与报告、多副本共享运行指标。Redis 可用时天然跨副本，
降级内存时行为一致（仅单进程）。与 SessionStore 同款模式：真实实现 + 自动降级。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

from athena.agent.policy.contracts import (
    ActionDecision,
    DataOrigin,
    EnvironmentMode,
    ExecutionProfile,
    ToolCallV2,
    ToolSpecV2,
)
from athena.agent.workflow.state import (
    OpsTaskPhase,
    OpsTaskState,
    OpsTaskStatus,
    TaskBudget,
)
from athena.api.session_store import PUBLIC_TENANT_ID, tenant_id_for
from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json
from athena.memory.evidence import Evidence
from athena.types import JSONValue

if TYPE_CHECKING:  # 避免运行时循环依赖：schemas 仅用于类型标注与序列化
    from athena.api.auth import TenantContext
    from athena.api.schemas import BenchmarkRunResponse, StepTrace
    from athena.api.services import TaskRecord

_ERROR_INDEX_KEY = "metrics:error:index"
_TOKENS_KEY = "metrics:tokens"


class TaskStateConflictError(RuntimeError):
    """A cache-backed task state write did not match the expected version."""


class EventCursorExpiredError(RuntimeError):
    """The requested event cursor predates the retained event history."""


@dataclass(frozen=True)
class TaskEvent:
    """A tenant-scoped, monotonic event used by later SSE delivery."""

    task_id: str
    tenant_id: str
    sequence: int
    event_type: str
    data: dict[str, JSONValue]
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.tenant_id.strip():
            raise ValueError("task_id and tenant_id must be non-empty")
        if not self.event_type.strip():
            raise ValueError("event_type must be non-empty")
        if self.sequence <= 0:
            raise ValueError("sequence must be positive")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


def _tenant_id(tenant: "TenantContext") -> str:
    tenant_id = getattr(tenant, "tenant_id", "")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant must provide a non-empty tenant_id")
    return tenant_id


class TaskStateRepository:
    """Phase 1 cache adapter for OpsTaskState with tenant isolation.

    This adapter intentionally does not claim distributed compare-and-set semantics.
    The explicit version check catches stale callers in one process and documents the
    contract that the PostgreSQL implementation will enforce transactionally in P4.
    """

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 3600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._cache = cache
        self._ttl = ttl_seconds
        self._lock = threading.RLock()

    @staticmethod
    def _key(tenant_id: str, task_id: str) -> str:
        return f"ops:v1:task-state:{tenant_id}:{task_id}"

    @staticmethod
    def _index_key(tenant_id: str) -> str:
        return f"ops:v1:task-state-index:{tenant_id}"

    def save(
        self,
        tenant: "TenantContext",
        state: OpsTaskState,
        *,
        expected_state_version: int | None = None,
    ) -> None:
        with self._lock:
            self._save_unlocked(
                tenant,
                state,
                expected_state_version=expected_state_version,
            )

    def _save_unlocked(
        self,
        tenant: "TenantContext",
        state: OpsTaskState,
        *,
        expected_state_version: int | None = None,
    ) -> None:
        tenant_id = _tenant_id(tenant)
        if state.tenant_id != tenant_id:
            raise PermissionError("task state tenant does not match request tenant")
        current = self.load(tenant, state.task_id)
        if expected_state_version is not None:
            if current is None or current.state_version != expected_state_version:
                raise TaskStateConflictError("task state version conflict")
        elif current is not None and state.state_version <= current.state_version:
            raise TaskStateConflictError("task state version must advance")
        cache_set_json(
            self._cache,
            self._key(tenant_id, state.task_id),
            self._state_to_dict(state),
            ttl_seconds=self._ttl,
        )
        index_key = self._index_key(tenant_id)
        task_ids = cache_get_json(self._cache, index_key) or []
        if state.task_id not in task_ids:
            task_ids.append(state.task_id)
            cache_set_json(self._cache, index_key, task_ids, ttl_seconds=self._ttl)

    def load(self, tenant: "TenantContext", task_id: str) -> OpsTaskState | None:
        tenant_id = _tenant_id(tenant)
        raw = cache_get_json(self._cache, self._key(tenant_id, task_id))
        if raw is None:
            return None
        state = self._state_from_dict(raw)
        if state.tenant_id != tenant_id:
            raise RuntimeError("task state tenant integrity violation")
        return state

    def list(self, tenant: "TenantContext") -> tuple[OpsTaskState, ...]:
        tenant_id = _tenant_id(tenant)
        task_ids = cache_get_json(self._cache, self._index_key(tenant_id)) or []
        return tuple(
            state
            for task_id in task_ids
            if (state := self.load(tenant, str(task_id))) is not None
        )

    def request_cancel(self, tenant: "TenantContext", task_id: str) -> OpsTaskState:
        with self._lock:
            state = self.load(tenant, task_id)
            if state is None:
                raise KeyError("task state not found")
            if state.status in {
                OpsTaskStatus.SUCCEEDED,
                OpsTaskStatus.FAILED,
                OpsTaskStatus.CANCELLED,
            }:
                return state
            cancelled = state.transition_to(OpsTaskStatus.CANCELLED)
            self.save(tenant, cancelled, expected_state_version=state.state_version)
            return cancelled

    @staticmethod
    def _state_to_dict(state: OpsTaskState) -> dict[str, object]:
        return {
            "task_id": state.task_id,
            "tenant_id": state.tenant_id,
            "objective": state.objective,
            "environment_id": state.environment_id,
            "environment_mode": state.environment_mode.value,
            "scope": state.scope,
            "tenant_policy_snapshot": state.tenant_policy_snapshot,
            "budget": asdict(state.budget),
            "execution_profile": state.execution_profile.value,
            "status": state.status.value,
            "phase": state.phase.value,
            "facts": list(state.facts),
            "hypotheses": list(state.hypotheses),
            "completed_actions": [asdict(action) for action in state.completed_actions],
            "failed_actions": [asdict(action) for action in state.failed_actions],
            "action_history": [asdict(action) for action in state.action_history],
            "skill_version_id": state.skill_version_id,
            "lease_owner": state.lease_owner,
            "lease_expires_at": (
                state.lease_expires_at.isoformat() if state.lease_expires_at else None
            ),
            "state_version": state.state_version,
        }

    @staticmethod
    def _state_from_dict(raw: dict[str, object]) -> OpsTaskState:
        budget = raw["budget"]
        if not isinstance(budget, dict):
            raise ValueError("invalid persisted task budget")
        lease_expires_at = raw.get("lease_expires_at")
        return OpsTaskState(
            task_id=str(raw["task_id"]),
            tenant_id=str(raw["tenant_id"]),
            objective=str(raw["objective"]),
            environment_id=str(raw["environment_id"]),
            environment_mode=EnvironmentMode(str(raw["environment_mode"])),
            scope=dict(raw.get("scope") or {}),  # type: ignore[arg-type]
            tenant_policy_snapshot=dict(raw.get("tenant_policy_snapshot") or {}),  # type: ignore[arg-type]
            budget=TaskBudget(
                remaining_steps=int(budget["remaining_steps"]),
                remaining_tokens=int(budget["remaining_tokens"]),
                remaining_time_ms=int(budget["remaining_time_ms"]),
            ),
            execution_profile=ExecutionProfile(str(raw["execution_profile"])),
            status=OpsTaskStatus(str(raw["status"])),
            phase=OpsTaskPhase(str(raw["phase"])),
            facts=tuple(raw.get("facts") or ()),  # type: ignore[arg-type]
            hypotheses=tuple(raw.get("hypotheses") or ()),  # type: ignore[arg-type]
            completed_actions=tuple(
                ActionDecision(**item) for item in raw.get("completed_actions", [])  # type: ignore[arg-type]
            ),
            failed_actions=tuple(
                ActionDecision(**item) for item in raw.get("failed_actions", [])  # type: ignore[arg-type]
            ),
            action_history=tuple(
                ActionDecision(**item) for item in raw.get("action_history", [])  # type: ignore[arg-type]
            ),
            skill_version_id=raw.get("skill_version_id") or None,  # type: ignore[arg-type]
            lease_owner=raw.get("lease_owner") or None,  # type: ignore[arg-type]
            lease_expires_at=(
                datetime.fromisoformat(str(lease_expires_at))
                if lease_expires_at
                else None
            ),
            state_version=int(raw.get("state_version", 0)),
        )


class TaskEventRepository:
    """Phase 1 tenant-scoped event history; SSE is added by P1-05."""

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 3600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._cache = cache
        self._ttl = ttl_seconds
        self._lock = threading.RLock()

    @staticmethod
    def _key(tenant_id: str, task_id: str) -> str:
        return f"ops:v1:task-events:{tenant_id}:{task_id}"

    def append(
        self,
        tenant: "TenantContext",
        task_id: str,
        event_type: str,
        data: dict[str, JSONValue],
    ) -> TaskEvent:
        tenant_id = _tenant_id(tenant)
        with self._lock:
            events = cache_get_json(self._cache, self._key(tenant_id, task_id)) or []
            sequence = int(events[-1]["sequence"]) + 1 if events else 1
            event = TaskEvent(
                task_id=task_id,
                tenant_id=tenant_id,
                sequence=sequence,
                event_type=event_type,
                data=data,
                created_at=datetime.now(UTC),
            )
            events.append(self._event_to_dict(event))
            cache_set_json(
                self._cache,
                self._key(tenant_id, task_id),
                events,
                ttl_seconds=self._ttl,
            )
            return event

    def list_after(
        self, tenant: "TenantContext", task_id: str, after_sequence: int = 0
    ) -> tuple[TaskEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        tenant_id = _tenant_id(tenant)
        with self._lock:
            raw_events = (
                cache_get_json(self._cache, self._key(tenant_id, task_id)) or []
            )
            events = tuple(self._event_from_dict(raw) for raw in raw_events)
        if after_sequence and not events:
            raise EventCursorExpiredError("EVENT_CURSOR_EXPIRED")
        if events and after_sequence < events[0].sequence - 1:
            raise EventCursorExpiredError("EVENT_CURSOR_EXPIRED")
        return tuple(event for event in events if event.sequence > after_sequence)

    @staticmethod
    def _event_to_dict(event: TaskEvent) -> dict[str, object]:
        return {
            "task_id": event.task_id,
            "tenant_id": event.tenant_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "data": event.data,
            "created_at": event.created_at.isoformat(),
        }

    @staticmethod
    def _event_from_dict(raw: dict[str, object]) -> TaskEvent:
        return TaskEvent(
            task_id=str(raw["task_id"]),
            tenant_id=str(raw["tenant_id"]),
            sequence=int(raw["sequence"]),
            event_type=str(raw["event_type"]),
            data=dict(raw.get("data") or {}),  # type: ignore[arg-type]
            created_at=datetime.fromisoformat(str(raw["created_at"])),
        )


class EvidenceStore:
    """Phase 1 cache adapter for evidence metadata and controlled content references."""

    def __init__(
        self,
        cache: CacheBackend,
        ttl_seconds: int = 3600,
        max_content_bytes: int = 512 * 1024,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_content_bytes <= 0:
            raise ValueError("max_content_bytes must be positive")
        self._cache = cache
        self._ttl = ttl_seconds
        self._max_content_bytes = max_content_bytes

    @staticmethod
    def _metadata_key(tenant_id: str, evidence_id: str) -> str:
        return f"ops:v1:evidence:{tenant_id}:{evidence_id}"

    @staticmethod
    def _content_key(tenant_id: str, evidence_id: str) -> str:
        return f"ops:v1:evidence-content:{tenant_id}:{evidence_id}"

    @staticmethod
    def _task_index_key(tenant_id: str, task_id: str) -> str:
        return f"ops:v1:task-evidence:{tenant_id}:{task_id}"

    def create(
        self,
        tenant: "TenantContext",
        *,
        task_id: str,
        evidence_type: str,
        source: str,
        data_origin: DataOrigin,
        summary: str,
        content: JSONValue,
        observed_at: datetime | None = None,
    ) -> Evidence:
        tenant_id = _tenant_id(tenant)
        evidence_id = f"evidence-{uuid.uuid4().hex}"
        captured_at = observed_at or datetime.now(UTC)
        controlled_content = self._controlled_content(content)
        serialized = self._serialize_content(controlled_content)
        evidence = Evidence(
            id=evidence_id,
            tenant_id=tenant_id,
            task_id=task_id,
            type=evidence_type,
            source=source,
            data_origin=data_origin,
            summary=summary,
            content_ref=f"cache-evidence://{evidence_id}",
            content_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            observed_at=captured_at,
            collected_at=datetime.now(UTC),
        )
        self.save(tenant, evidence, controlled_content)
        return evidence

    def save(
        self, tenant: "TenantContext", evidence: Evidence, content: JSONValue | None
    ) -> None:
        tenant_id = _tenant_id(tenant)
        if evidence.tenant_id != tenant_id:
            raise PermissionError("evidence tenant does not match request tenant")
        cache_set_json(
            self._cache,
            self._metadata_key(tenant_id, evidence.id),
            self._evidence_to_dict(evidence),
            ttl_seconds=self._ttl,
        )
        if content is not None:
            cache_set_json(
                self._cache,
                self._content_key(tenant_id, evidence.id),
                content,
                ttl_seconds=self._ttl,
            )
        index_key = self._task_index_key(tenant_id, evidence.task_id)
        evidence_ids = cache_get_json(self._cache, index_key) or []
        if evidence.id not in evidence_ids:
            evidence_ids.append(evidence.id)
        cache_set_json(self._cache, index_key, evidence_ids, ttl_seconds=self._ttl)

    def get(self, tenant: "TenantContext", evidence_id: str) -> Evidence | None:
        tenant_id = _tenant_id(tenant)
        raw = cache_get_json(self._cache, self._metadata_key(tenant_id, evidence_id))
        if raw is None:
            return None
        evidence = self._evidence_from_dict(raw)
        if evidence.tenant_id != tenant_id:
            raise RuntimeError("evidence tenant integrity violation")
        return evidence

    def list_for_task(
        self, tenant: "TenantContext", task_id: str
    ) -> tuple[Evidence, ...]:
        tenant_id = _tenant_id(tenant)
        evidence_ids = (
            cache_get_json(self._cache, self._task_index_key(tenant_id, task_id)) or []
        )
        return tuple(
            evidence
            for evidence_id in evidence_ids
            if (evidence := self.get(tenant, str(evidence_id))) is not None
        )

    def get_content(
        self, tenant: "TenantContext", evidence_id: str
    ) -> JSONValue | None:
        tenant_id = _tenant_id(tenant)
        if self.get(tenant, evidence_id) is None:
            return None
        return cache_get_json(self._cache, self._content_key(tenant_id, evidence_id))

    @staticmethod
    def _serialize_content(content: JSONValue) -> str:
        import json

        return json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def _controlled_content(self, content: JSONValue) -> JSONValue:
        sanitized = self._redact_content(content)
        serialized = self._serialize_content(sanitized)
        if len(serialized.encode("utf-8")) <= self._max_content_bytes:
            return sanitized
        data_origin = (
            sanitized.get("data_origin") if isinstance(sanitized, dict) else None
        )
        preview_bytes = serialized.encode("utf-8")[: self._max_content_bytes]
        preview = preview_bytes.decode("utf-8", errors="ignore")
        return {
            "data_origin": data_origin,
            "truncated": True,
            "original_content_hash": hashlib.sha256(
                serialized.encode("utf-8")
            ).hexdigest(),
            "preview": preview,
        }

    @classmethod
    def _redact_content(cls, value: JSONValue) -> JSONValue:
        sensitive = re.compile(
            r"token|secret|password|authorization|cookie|api[_-]?key|credential",
            re.IGNORECASE,
        )
        if isinstance(value, dict):
            return {
                str(key): (
                    "[REDACTED]"
                    if sensitive.search(str(key))
                    else cls._redact_content(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact_content(item) for item in value]
        if isinstance(value, str):
            value = re.sub(
                r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
                "Bearer [REDACTED]",
                value,
            )
            return re.sub(
                r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+",
                lambda match: f"{match.group(1)}=[REDACTED]",
                value,
            )
        return value

    @staticmethod
    def _evidence_to_dict(evidence: Evidence) -> dict[str, object]:
        return {
            "id": evidence.id,
            "tenant_id": evidence.tenant_id,
            "task_id": evidence.task_id,
            "type": evidence.type,
            "source": evidence.source,
            "data_origin": evidence.data_origin.value,
            "summary": evidence.summary,
            "content_ref": evidence.content_ref,
            "content_hash": evidence.content_hash,
            "observed_at": evidence.observed_at.isoformat(),
            "collected_at": evidence.collected_at.isoformat(),
        }

    @staticmethod
    def _evidence_from_dict(raw: dict[str, object]) -> Evidence:
        return Evidence(
            id=str(raw["id"]),
            tenant_id=str(raw["tenant_id"]),
            task_id=str(raw["task_id"]),
            type=str(raw["type"]),
            source=str(raw["source"]),
            data_origin=DataOrigin(str(raw["data_origin"])),
            summary=str(raw["summary"]),
            content_ref=raw.get("content_ref") or None,  # type: ignore[arg-type]
            content_hash=str(raw["content_hash"]),
            observed_at=datetime.fromisoformat(str(raw["observed_at"])),
            collected_at=datetime.fromisoformat(str(raw["collected_at"])),
        )


class CacheEvidenceSink:
    """Bind ToolRuntime evidence writes to one task's verified data origin.

    The caller must pass the origin observed by the provider adapter.  In
    particular, ``LIVE`` is never inferred from an environment's intended mode.
    """

    def __init__(
        self,
        store: EvidenceStore,
        tenant: "TenantContext",
        data_origin: DataOrigin,
    ) -> None:
        self._store = store
        self._tenant = tenant
        self._tenant_id = _tenant_id(tenant)
        self._data_origin = data_origin

    def persist(
        self, call: ToolCallV2, spec: ToolSpecV2, data: JSONValue | None
    ) -> tuple[str, ...]:
        if call.tenant_id != self._tenant_id:
            raise PermissionError("tool call tenant does not match evidence sink")
        evidence = self._store.create(
            self._tenant,
            task_id=call.task_id,
            evidence_type=self._evidence_type(spec.name),
            source=spec.name,
            data_origin=self._data_origin,
            summary=f"observation collected by {spec.name}",
            content=data,
        )
        return (evidence.id,)

    @staticmethod
    def _evidence_type(tool_name: str) -> str:
        if ".logs." in tool_name:
            return "log"
        if ".events." in tool_name:
            return "event"
        if ".query" in tool_name:
            return "metric"
        return "resource_snapshot"


class ObservedEvidenceSink:
    """Persist V2 tool results using the provider-reported data origin."""

    def __init__(
        self,
        store: EvidenceStore,
        task_repository: TaskStateRepository | None = None,
    ) -> None:
        self._store = store
        self._tasks = task_repository

    def persist(
        self, call: ToolCallV2, spec: ToolSpecV2, data: JSONValue | None
    ) -> tuple[str, ...]:
        if not isinstance(data, dict):
            raise ValueError("EVIDENCE_ORIGIN_MISSING")
        raw_origin = data.get("data_origin")
        try:
            data_origin = DataOrigin(str(raw_origin))
        except ValueError as exc:
            raise ValueError("EVIDENCE_ORIGIN_INVALID") from exc

        from athena.api.auth import TenantContext

        tenant = TenantContext(tenant_id=call.tenant_id, api_key=None, roles=())
        if self._tasks is not None:
            state = self._tasks.load(tenant, call.task_id)
            if state is None:
                raise ValueError("EVIDENCE_TASK_NOT_FOUND")
            if state.environment_mode.value != data_origin.value:
                raise ValueError("NON_LIVE_EVIDENCE_IN_LIVE_TASK")
        evidence = self._store.create(
            tenant,
            task_id=call.task_id,
            evidence_type=CacheEvidenceSink._evidence_type(spec.name),
            source=spec.name,
            data_origin=data_origin,
            summary=self._summary(spec.name, data),
            content=data,
        )
        return (evidence.id,)

    @staticmethod
    def _summary(tool_name: str, data: dict[str, JSONValue]) -> str:
        if tool_name == "k8s.pod.list":
            items = data.get("items")
            count = len(items) if isinstance(items, list) else 0
            return f"Kubernetes pod snapshot collected ({count} items)"
        if tool_name == "k8s.pod.get":
            return "Kubernetes pod detail collected"
        if tool_name == "k8s.events.list":
            items = data.get("items")
            count = len(items) if isinstance(items, list) else 0
            return f"Kubernetes event snapshot collected ({count} items)"
        if tool_name == "k8s.logs.read":
            return "Kubernetes container log tail collected"
        return f"Observation collected by {tool_name}"


class TaskStore:
    """
    基于缓存后端的任务仓库。

    键结构：task:{id} 存单任务 JSON（含步骤轨迹）。
    仅按 id 读取，无需全量列举，因此不维护索引，交由 TTL 回收。
    """

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 3600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._cache = cache
        self._ttl = ttl_seconds

    @staticmethod
    def _key_for_tenant(tenant_id: str, task_id: str) -> str:
        """Return the tenant-partitioned cache key for a legacy task record.

        ``public`` keeps the original key shape for local-demo compatibility.
        Authenticated tenants never fall back to this shared key, so a guessed
        task id cannot cross the tenant boundary.
        """
        if tenant_id == PUBLIC_TENANT_ID:
            return f"task:{quote(task_id, safe='')}"
        return "task:v2:{tenant_id}:{task_id}".format(
            tenant_id=quote(tenant_id, safe=""),
            task_id=quote(task_id, safe=""),
        )

    def _key(self, task_id: str, tenant: "TenantContext | None" = None) -> str:
        return self._key_for_tenant(tenant_id_for(tenant), task_id)

    @staticmethod
    def _record_tenant_id(record: "TaskRecord") -> str:
        tenant_id = getattr(record, "tenant_id", "")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("task record must provide a non-empty tenant_id")
        return tenant_id.strip()

    def save(
        self, record: "TaskRecord", *, tenant: "TenantContext | None" = None
    ) -> None:
        """写入/更新一条任务记录（每次状态变更都应调用）。"""
        record_tenant_id = self._record_tenant_id(record)
        if tenant is not None and record_tenant_id != tenant_id_for(tenant):
            raise PermissionError("task record tenant does not match request tenant")
        cache_set_json(
            self._cache,
            self._key_for_tenant(record_tenant_id, record.task_id),
            self._to_dict(record),
            ttl_seconds=self._ttl,
        )

    def get(
        self, task_id: str, *, tenant: "TenantContext | None" = None
    ) -> "TaskRecord | None":
        raw = cache_get_json(self._cache, self._key(task_id, tenant))
        if raw is None:
            return None
        record = self._from_dict(raw)
        if record.tenant_id != tenant_id_for(tenant):
            raise RuntimeError("task record tenant integrity violation")
        return record

    @staticmethod
    def _to_dict(record: "TaskRecord") -> dict:
        return {
            "task_id": record.task_id,
            "status": record.status,
            "tenant_id": record.tenant_id,
            "answer": record.answer,
            "steps": [step.model_dump() for step in record.steps],
            "error": record.error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _from_dict(raw: dict) -> "TaskRecord":
        from athena.api.schemas import StepTrace
        from athena.api.services import TaskRecord

        steps: list[StepTrace] = [
            StepTrace.model_validate(item) for item in raw.get("steps", [])
        ]
        return TaskRecord(
            task_id=raw["task_id"],
            status=raw["status"],
            tenant_id=str(raw.get("tenant_id") or "public"),
            answer=raw.get("answer"),
            steps=steps,
            error=raw.get("error"),
            created_at=raw.get("created_at", time.time()),
            updated_at=raw.get("updated_at", time.time()),
        )


class MetricsStore:
    """
    基于缓存后端的运行指标仓库：错误分布 + Token 累计。

    错误分布用 incr 计数 + 名称索引；Token 用读改写累加。多副本共享同一 Redis 时天然聚合。
    """

    def __init__(self, cache: CacheBackend) -> None:
        self._cache = cache

    def incr_error(self, error_name: str) -> None:
        """错误计数 +1，并把错误名登记进索引以便枚举分布。"""
        self._cache.incr(f"metrics:error:{error_name}")
        names = cache_get_json(self._cache, _ERROR_INDEX_KEY) or []
        if error_name not in names:
            names.append(error_name)
            cache_set_json(self._cache, _ERROR_INDEX_KEY, names)

    def error_distribution(self) -> dict[str, int]:
        """读取全部错误名及其计数。"""
        names = cache_get_json(self._cache, _ERROR_INDEX_KEY) or []
        distribution: dict[str, int] = {}
        for name in names:
            raw = self._cache.get(f"metrics:error:{name}")
            distribution[name] = int(raw) if raw is not None else 0
        return distribution

    def add_tokens(self, amount: int) -> None:
        """Token 累计（读改写，支持任意增量）。"""
        if amount <= 0:
            return
        current = self.token_usage()
        cache_set_json(self._cache, _TOKENS_KEY, current + amount)

    def token_usage(self) -> int:
        return int(cache_get_json(self._cache, _TOKENS_KEY) or 0)


class BenchmarkStore:
    """Tenant-scoped benchmark reports stored under ``benchmark:{tenant}:{run}``."""

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 86400) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._cache = cache
        self._ttl = ttl_seconds

    def _key(self, tenant: "TenantContext", run_id: str) -> str:
        return f"benchmark:{_tenant_id(tenant)}:{run_id}"

    def save(self, tenant: "TenantContext", report: "BenchmarkRunResponse") -> None:
        cache_set_json(
            self._cache,
            self._key(tenant, report.run_id),
            report.model_dump(),
            ttl_seconds=self._ttl,
        )

    def get(
        self, tenant: "TenantContext", run_id: str
    ) -> "BenchmarkRunResponse | None":
        raw = cache_get_json(self._cache, self._key(tenant, run_id))
        if raw is None:
            return None
        from athena.api.schemas import BenchmarkRunResponse

        return BenchmarkRunResponse.model_validate(raw)
