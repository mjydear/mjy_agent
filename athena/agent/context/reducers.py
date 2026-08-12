"""Deterministic reducers used before evidence enters a model context."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from athena.memory.evidence import Evidence
from athena.types import JSONValue

_ERROR_CODE_PATTERN = re.compile(r"\b(?:[A-Z][A-Z0-9_]{2,}|[A-Z][a-z]+Error)\b")
_STACK_FRAME_PATTERN = re.compile(r"^\s*(?:File .+|at .+|#\d+ .+)$", re.MULTILINE)
_K8S_KEYS = frozenset(
    {
        "allocatable",
        "apiVersion",
        "available",
        "availableReplicas",
        "cluster_ip",
        "conditions",
        "containerStatuses",
        "containers",
        "count",
        "data_origin",
        "desired",
        "error_code",
        "events",
        "healthy",
        "image",
        "item",
        "items",
        "kind",
        "lastState",
        "message",
        "metadata",
        "name",
        "namespace",
        "node",
        "nodeName",
        "phase",
        "pod",
        "pressure",
        "ready",
        "readyReplicas",
        "reason",
        "resource_id",
        "restartCount",
        "restart_count",
        "restarts",
        "start_time",
        "state",
        "status",
        "time_range",
        "type",
        "uid",
        "unit",
        "updated",
        "updatedReplicas",
    }
)
_PROMETHEUS_KEYS = frozenset(
    {
        "aggregate",
        "aggregation",
        "anomalies",
        "available",
        "avg",
        "count",
        "data_origin",
        "end",
        "error_code",
        "latest",
        "max",
        "metric",
        "min",
        "name",
        "query",
        "query_window",
        "resource_id",
        "series",
        "source",
        "start",
        "step",
        "time_range",
        "unit",
        "value",
        "values",
        "window",
    }
)


@dataclass(frozen=True)
class ReductionStats:
    input_characters: int
    output_characters: int
    duplicate_lines_collapsed: int
    stack_fingerprints: tuple[str, ...]
    folded_items: int = 0
    omitted_fields: int = 0


class EvidenceReducer:
    """Reduce untrusted evidence without replacing the underlying record."""

    def reduce_log(
        self, content: str, *, max_lines: int = 80
    ) -> tuple[str, ReductionStats]:
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        counts = Counter(lines)
        unique_lines: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line not in seen:
                seen.add(line)
                suffix = f" [repeated {counts[line]} times]" if counts[line] > 1 else ""
                unique_lines.append(f"{line}{suffix}")
        reduced_lines = unique_lines[:max_lines]
        omitted_lines = max(0, len(unique_lines) - max_lines)
        if omitted_lines:
            reduced_lines.append(f"[truncated {omitted_lines} unique lines]")
        reduced = "\n".join(reduced_lines)
        fingerprints = tuple(
            sorted(
                {
                    hashlib.sha256(match.group(0).strip().encode("utf-8")).hexdigest()[
                        :16
                    ]
                    for match in _STACK_FRAME_PATTERN.finditer(content)
                }
            )
        )
        return reduced, ReductionStats(
            input_characters=len(content),
            output_characters=len(reduced),
            duplicate_lines_collapsed=len(lines) - len(unique_lines),
            stack_fingerprints=fingerprints,
            folded_items=omitted_lines,
        )

    def reduce_kubernetes(
        self, content: JSONValue, *, max_items: int = 20
    ) -> tuple[JSONValue, ReductionStats]:
        """Keep resource identity and diagnostic state from large K8s objects."""
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        before = _stable_json(content)
        reduced, omitted, folded = self._project(
            content, allowed_keys=_K8S_KEYS, max_items=max_items
        )
        after = _stable_json(reduced)
        return reduced, ReductionStats(
            input_characters=len(before),
            output_characters=len(after),
            duplicate_lines_collapsed=0,
            stack_fingerprints=(),
            folded_items=folded,
            omitted_fields=omitted,
        )

    def reduce_prometheus(
        self, content: JSONValue, *, max_series: int = 12
    ) -> tuple[JSONValue, ReductionStats]:
        """Keep query/window/aggregates while folding raw metric samples."""
        if max_series <= 0:
            raise ValueError("max_series must be positive")
        before = _stable_json(content)
        reduced, omitted, folded = self._project(
            content, allowed_keys=_PROMETHEUS_KEYS, max_items=max_series
        )
        if isinstance(content, Mapping):
            samples = content.get("values")
            aggregate = self._sample_aggregate(samples)
            if aggregate is not None and isinstance(reduced, dict):
                reduced["sample_aggregate"] = aggregate
                reduced.pop("values", None)
                sample_count = len(samples) if _is_sequence(samples) else 0
                folded += sample_count
        after = _stable_json(reduced)
        return reduced, ReductionStats(
            input_characters=len(before),
            output_characters=len(after),
            duplicate_lines_collapsed=0,
            stack_fingerprints=(),
            folded_items=folded,
            omitted_fields=omitted,
        )

    def reduce_evidence(
        self,
        evidence: Evidence,
        content: JSONValue | None,
        *,
        time_range: JSONValue | None = None,
    ) -> tuple[dict[str, JSONValue], ReductionStats]:
        """Create the prompt-safe summary used for a single evidence record."""
        raw_text = evidence.summary
        if content is not None:
            raw_text = f"{raw_text}\n{_stable_json(content)}"
        error_codes = set(_ERROR_CODE_PATTERN.findall(raw_text))
        explicit_error = _find_value(content, "error_code")
        if isinstance(explicit_error, str) and explicit_error:
            error_codes.add(explicit_error)

        reduction = "reference"
        reduced_content: JSONValue | None = None
        stats = ReductionStats(0, 0, 0, ())
        if content is not None:
            evidence_kind = evidence.type.lower()
            source = evidence.source.lower()
            if "log" in evidence_kind or ".logs." in source:
                reduction = "log"
                log_content = _log_text(content)
                reduced_content, stats = self.reduce_log(log_content)
            elif "metric" in evidence_kind or "prometheus" in source:
                reduction = "prometheus"
                reduced_content, stats = self.reduce_prometheus(content)
            elif (
                source.startswith("k8s.")
                or "kubernetes" in source
                or evidence_kind in {"event", "resource_snapshot", "k8s"}
            ):
                reduction = "kubernetes"
                reduced_content, stats = self.reduce_kubernetes(content)
            else:
                reduction = "generic"
                reduced_content, stats = self._reduce_generic(content)

        resource_id = _find_value(content, "resource_id")
        if not isinstance(resource_id, str) or not resource_id:
            resource_id = _resource_id(content)
        content_time_range = _find_value(content, "time_range")
        preserved_time_range = (
            content_time_range if content_time_range is not None else time_range
        )
        ordered_codes = sorted(error_codes)
        result: dict[str, JSONValue] = {
            "evidence_id": evidence.id,
            "type": evidence.type,
            "source": evidence.source,
            "data_origin": evidence.data_origin.value,
            "summary": evidence.summary,
            "content_ref": evidence.content_ref,
            "observed_at": evidence.observed_at.isoformat(),
            "resource_id": resource_id,
            "time_range": preserved_time_range,
            "error_code": explicit_error
            or (ordered_codes[0] if ordered_codes else None),
            "error_codes": ordered_codes,
        }
        if reduced_content is not None:
            result["content"] = {
                "kind": "untrusted_evidence",
                "reduction": reduction,
                "value": reduced_content,
            }
        if stats.stack_fingerprints:
            result["stack_fingerprints"] = list(stats.stack_fingerprints)
        return result, stats

    def summarize_evidence(self, evidence: Evidence) -> dict[str, JSONValue]:
        """Create a reference-only summary when no controlled loader is present."""
        summary, _ = self.reduce_evidence(evidence, None)
        return summary

    def _reduce_generic(
        self, content: JSONValue, *, max_characters: int = 2000
    ) -> tuple[JSONValue, ReductionStats]:
        serialized = _stable_json(content)
        omitted = 0
        reduced: JSONValue = content
        if len(serialized) > max_characters:
            reduced = f"{serialized[:max_characters]}[truncated]"
            omitted = 1
        after = _stable_json(reduced)
        return reduced, ReductionStats(
            input_characters=len(serialized),
            output_characters=len(after),
            duplicate_lines_collapsed=0,
            stack_fingerprints=(),
            omitted_fields=omitted,
        )

    def _project(
        self,
        value: JSONValue,
        *,
        allowed_keys: frozenset[str],
        max_items: int,
        depth: int = 0,
    ) -> tuple[JSONValue, int, int]:
        if depth >= 6:
            return "[nested value omitted]", 1, 0
        if isinstance(value, Mapping):
            result: dict[str, JSONValue] = {}
            omitted = 0
            folded = 0
            for raw_key in sorted(value, key=str):
                key = str(raw_key)
                if key not in allowed_keys:
                    omitted += 1
                    continue
                projected, child_omitted, child_folded = self._project(
                    value[raw_key],
                    allowed_keys=allowed_keys,
                    max_items=max_items,
                    depth=depth + 1,
                )
                result[key] = projected
                omitted += child_omitted
                folded += child_folded
            return result, omitted, folded
        if _is_sequence(value):
            values = list(value)
            visible = values[:max_items]
            result_items: list[JSONValue] = []
            omitted = 0
            folded = max(0, len(values) - len(visible))
            for item in visible:
                projected, child_omitted, child_folded = self._project(
                    item,
                    allowed_keys=allowed_keys,
                    max_items=max_items,
                    depth=depth + 1,
                )
                result_items.append(projected)
                omitted += child_omitted
                folded += child_folded
            return result_items, omitted, folded
        if isinstance(value, str) and len(value) > 512:
            return f"{value[:512]}[truncated]", 1, 0
        return value, 0, 0

    @staticmethod
    def _sample_aggregate(value: object) -> dict[str, JSONValue] | None:
        if not _is_sequence(value):
            return None
        numbers: list[float] = []
        for sample in value:
            candidate: object = sample
            if _is_sequence(sample):
                pair = list(sample)
                candidate = pair[-1] if pair else None
            try:
                if isinstance(candidate, bool) or not isinstance(
                    candidate, (str, int, float)
                ):
                    continue
                numbers.append(float(candidate))
            except (TypeError, ValueError):
                continue
        if not numbers:
            return None
        return {
            "count": len(numbers),
            "min": min(numbers),
            "max": max(numbers),
            "avg": sum(numbers) / len(numbers),
            "latest": numbers[-1],
        }


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _is_sequence(value: object) -> TypeGuard[Sequence[JSONValue]]:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _find_value(value: object, key: str) -> JSONValue | None:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if candidate is not None:
            return candidate  # type: ignore[return-value]
        for child_key in sorted(value, key=str):
            found = _find_value(value[child_key], key)
            if found is not None:
                return found
    elif _is_sequence(value):
        for child in value:
            found = _find_value(child, key)
            if found is not None:
                return found
    return None


def _resource_id(content: object) -> str | None:
    candidate: object = content
    if _is_sequence(candidate):
        candidates = list(candidate)
        candidate = candidates[0] if candidates else None
    if isinstance(candidate, Mapping):
        candidate = candidate.get("item", candidate)
        if isinstance(candidate, Mapping) and "items" in candidate:
            items = candidate.get("items")
            if _is_sequence(items) and items:
                candidate = list(items)[0]
    if not isinstance(candidate, Mapping):
        return None
    metadata = candidate.get("metadata")
    if isinstance(metadata, Mapping):
        candidate = metadata
    name = candidate.get("name")
    namespace = candidate.get("namespace")
    if isinstance(name, str) and name:
        if isinstance(namespace, str) and namespace:
            return f"{namespace}/{name}"
        return name
    pod = candidate.get("pod")
    return pod if isinstance(pod, str) and pod else None


def _log_text(content: JSONValue) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        nested = content.get("content")
        if isinstance(nested, str):
            return nested
    return _stable_json(content)
