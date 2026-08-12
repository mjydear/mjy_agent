"""Persist a safe Diagnosis Outcome before a durable task is checkpointed."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import replace

from athena.api.repositories import PersistedTask
from athena.application.diagnosis_outcome_service import (
    DiagnosisOutcomeService,
    DiagnosisOutcomeServiceError,
)
from athena.application.durable_worker import TaskHandler, WorkerOutcome

logger = logging.getLogger(__name__)

_SEVERITY_CONFIDENCE = {
    "critical": 0.95,
    "high": 0.85,
    "medium": 0.70,
    "low": 0.50,
}


class DurableOutcomeRecordingHandler:
    """Decorate a readonly handler with an immutable Outcome write.

    The decorator is deliberately placed before ``DurableTaskWorker``'s
    checkpoint seam.  A task cannot be marked succeeded while its Outcome is
    missing, and a retry is harmless because the fact store is idempotent.
    """

    def __init__(
        self,
        delegate: TaskHandler,
        outcomes: DiagnosisOutcomeService,
        *,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        if retry_delay_seconds < 0 or not math.isfinite(retry_delay_seconds):
            raise ValueError("retry_delay_seconds must be finite and non-negative")
        self._delegate = delegate
        self._outcomes = outcomes
        self._retry_delay_seconds = retry_delay_seconds

    async def __call__(self, task: PersistedTask) -> WorkerOutcome:
        result = await self._delegate(task)
        if result.retry_delay_seconds is not None or result.status != "succeeded":
            return result

        outcome_input = _outcome_input(result.state)
        try:
            outcome = await self._outcomes.finalize(
                task.tenant_id,
                task.task_id,
                **outcome_input,
            )
        except DiagnosisOutcomeServiceError as exc:
            # The worker owns retry/dead-letter policy. Do not expose an
            # exception string or any report content in the durable state.
            logger.warning(
                "diagnosis outcome persistence failed task=%s code=%s",
                task.task_id,
                exc.error_code,
            )
            return WorkerOutcome(
                state={
                    **result.state,
                    "error_code": "DIAGNOSIS_OUTCOME_PERSIST_FAILED",
                    "outcome_error_code": exc.error_code,
                },
                phase="report",
                status="failed",
                event_type="task.failed",
                retry_delay_seconds=self._retry_delay_seconds,
                error_code="DIAGNOSIS_OUTCOME_PERSIST_FAILED",
            )
        except Exception:  # noqa: BLE001 - transient adapters must be retried
            logger.exception("unexpected diagnosis outcome persistence failure task=%s", task.task_id)
            return WorkerOutcome(
                state={
                    **result.state,
                    "error_code": "DIAGNOSIS_OUTCOME_PERSIST_FAILED",
                },
                phase="report",
                status="failed",
                event_type="task.failed",
                retry_delay_seconds=self._retry_delay_seconds,
                error_code="DIAGNOSIS_OUTCOME_PERSIST_FAILED",
            )

        return replace(
            result,
            state={
                **result.state,
                "diagnosis_outcome_id": outcome.outcome_id,
                "diagnosis_outcome_evidence_sufficient": outcome.evidence_sufficient,
            },
        )


def _outcome_input(state: Mapping[str, object]) -> dict[str, object]:
    evidence_ids = _string_values(state.get("evidence_ids"))
    root_cause = _root_cause(state.get("root_causes"))
    recommendation = _recommendation(state)
    sufficient = bool(evidence_ids and root_cause and recommendation)
    return {
        "root_cause": root_cause if sufficient else None,
        "supporting_evidence_ids": evidence_ids,
        "remediation_recommendation": recommendation if sufficient else None,
        "confidence": _confidence(state, sufficient),
        "evidence_sufficient": sufficient,
    }


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return ()
    values: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in values:
            values.append(item.strip())
    return tuple(values[:100])


def _root_cause(value: object) -> str | None:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return None
    candidates: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        for key in ("root_cause", "probable_causes"):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                candidates.append(raw.strip())
            elif isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, Mapping)):
                candidates.extend(
                    str(part).strip()
                    for part in raw
                    if isinstance(part, str) and part.strip()
                )
    return "; ".join(dict.fromkeys(candidates))[:4000] or None


def _recommendation(state: Mapping[str, object]) -> str | None:
    report = state.get("readonly_report")
    values: list[str] = []
    if isinstance(report, Mapping):
        raw_actions = report.get("actions")
        if isinstance(raw_actions, Iterable) and not isinstance(raw_actions, (str, bytes, Mapping)):
            values.extend(
                item.strip()
                for item in raw_actions
                if isinstance(item, str) and item.strip()
            )
        if not values:
            raw_findings = report.get("findings")
            if isinstance(raw_findings, Iterable) and not isinstance(raw_findings, (str, bytes, Mapping)):
                for finding in raw_findings:
                    if not isinstance(finding, Mapping):
                        continue
                    actions = finding.get("recommended_actions")
                    if isinstance(actions, Iterable) and not isinstance(actions, (str, bytes, Mapping)):
                        values.extend(
                            item.strip()
                            for item in actions
                            if isinstance(item, str) and item.strip()
                        )
    return "; ".join(dict.fromkeys(values))[:4000] or None


def _confidence(state: Mapping[str, object], sufficient: bool) -> float:
    if not sufficient:
        return 0.0
    value = state.get("confidence")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if math.isfinite(numeric):
            return min(1.0, max(0.0, numeric))
    root_causes = state.get("root_causes")
    severities: list[float] = []
    if isinstance(root_causes, Iterable) and not isinstance(root_causes, (str, bytes, Mapping)):
        for item in root_causes:
            if isinstance(item, Mapping):
                severity = item.get("severity")
                if isinstance(severity, str):
                    score = _SEVERITY_CONFIDENCE.get(severity.lower())
                    if score is not None:
                        severities.append(score)
    return max(severities, default=0.5)


__all__ = ["DurableOutcomeRecordingHandler"]
