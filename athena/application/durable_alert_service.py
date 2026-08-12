"""Durable Alertmanager ingress that creates tasks without synchronous diagnosis."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict

from athena.api.repositories import (
    AlertAcceptance,
    AlertTaskCreate,
    TaskCreate,
    TaskRepository,
)
from athena.integration.alert_webhook import AlertWebhookParser, AlertWebhookPayload


_ALERT_WORKFLOW_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "image_pull",
        ("imagepullbackoff", "errimagepull", "imagepull", "image_pull"),
    ),
    (
        "resource_pressure",
        (
            "resourcepressure",
            "failedscheduling",
            "unschedulable",
            "nodepressure",
            "memorypressure",
            "diskpressure",
            "pidpressure",
            "insufficient_cpu",
            "insufficient_memory",
        ),
    ),
    ("pod_pending", ("podpending", "pod_pending", "pending")),
    ("crashloop", ("crashloopbackoff", "crashloop", "restart")),
)


def _workflow_type_for_alert(alert_name: str) -> str:
    """Map the bounded alert vocabulary to a readonly diagnosis workflow."""

    normalized = re.sub(r"[^a-z0-9]+", "", alert_name.strip().lower())
    for workflow, markers in _ALERT_WORKFLOW_MARKERS:
        if any(marker.replace("_", "") in normalized for marker in markers):
            return workflow
    # The initial product scope is Pod diagnosis. Unknown alert names must not
    # silently receive the CrashLoop handler.
    return "unsupported"


class DurableAlertService:
    def __init__(
        self,
        tasks: TaskRepository,
        *,
        environment_mode: str,
        policy_snapshot: dict[str, object],
        config_snapshot: dict[str, object],
        allow_simplified: bool,
    ) -> None:
        self._tasks = tasks
        self._environment_mode = environment_mode
        self._policy_snapshot = policy_snapshot
        self._config_snapshot = config_snapshot
        self._allow_simplified = allow_simplified

    async def ingest(
        self,
        payload: object,
        *,
        tenant_id: str,
        integration_id: str,
        traceparent: str | None,
    ) -> dict[str, object]:
        try:
            alerts = AlertWebhookParser().parse_all(
                payload, allow_simplified=self._allow_simplified
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        records = [
            await self._accept_alert(
                alert,
                tenant_id=tenant_id,
                integration_id=integration_id,
                traceparent=traceparent,
            )
            for alert in alerts
        ]
        if len(records) == 1:
            return {**records[0], "processed_count": 1}
        return {
            "status": "accepted",
            "processed_count": len(records),
            "alerts": records,
        }

    async def _accept_alert(
        self,
        alert: AlertWebhookPayload,
        *,
        tenant_id: str,
        integration_id: str,
        traceparent: str | None,
    ) -> dict[str, object]:
        normalized = asdict(alert)
        payload_hash = self._hash(normalized)
        fingerprint = self._fingerprint(alert)
        task_id = f"ops-{uuid.uuid4().hex}"
        workflow_type = _workflow_type_for_alert(alert.alert_name)
        command = AlertTaskCreate(
            task=TaskCreate(
                task_id=task_id,
                tenant_id=tenant_id,
                objective=f"诊断 {alert.namespace} 命名空间告警 {alert.alert_name}",
                environment_id="default",
                environment_mode=self._environment_mode,
                scope={
                    "namespace": alert.namespace,
                    "alert_name": alert.alert_name,
                },
                policy_snapshot=dict(self._policy_snapshot),
                config_snapshot=dict(self._config_snapshot),
                budget={
                    "remaining_steps": 4,
                    "remaining_tokens": 6000,
                    "remaining_time_ms": 30000,
                },
                execution_profile="bounded_policy_loop",
                workflow_type=workflow_type,
                trigger_type="alertmanager",
                trigger_ref=fingerprint,
                traceparent=traceparent,
            ),
            integration_id=integration_id,
            payload_hash=payload_hash,
            canonical_fingerprint=fingerprint,
            payload=normalized,
            external_event_id=str(alert.labels.get("fingerprint", "")) or None,
        )
        accepted = await self._tasks.create_alert_task(command)
        return self._response(alert, accepted)

    @staticmethod
    def _hash(value: dict[str, object]) -> str:
        encoded = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _fingerprint(self, alert: AlertWebhookPayload) -> str:
        stable = {
            "version": "v1",
            "alert_name": alert.alert_name,
            "namespace": alert.namespace,
            "pod": alert.pod,
            "deployment": alert.deployment,
            "severity": alert.severity,
        }
        return self._hash(stable)

    @staticmethod
    def _response(
        alert: AlertWebhookPayload, accepted: AlertAcceptance
    ) -> dict[str, object]:
        return {
            "status": "accepted",
            "receipt_id": accepted.receipt_id,
            "task_id": accepted.task.task_id,
            "task_status": accepted.task.status,
            "duplicate": accepted.duplicate,
            "created": accepted.created,
            "alert_name": alert.alert_name,
            "severity": alert.severity,
            "namespace": alert.namespace,
            "data_origin": accepted.task.environment_mode,
        }
