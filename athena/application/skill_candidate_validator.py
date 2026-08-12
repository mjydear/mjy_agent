"""Deterministic Schema and security gates for persisted Skill Candidates."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Protocol

from athena.agent.policy.contracts import RiskLevel, ToolSpecV2
from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    REJECTED_STATUS,
    SkillCandidate,
)
from athena.learning.skill_validation import (
    CandidateValidationCategory,
    CandidateValidationReport,
    CandidateValidationViolation,
    SKILL_CANDIDATE_SCHEMA_VERSION,
    SKILL_CANDIDATE_VALIDATOR_VERSION,
    candidate_validation_digest,
    candidate_validation_report_id,
)
from athena.runtime.learning import TrajectoryStatus, TrajectorySummary
from athena.runtime.models import utc_now
from athena.runtime.tool_gateway import SERVER_CONTROLLED_ARGUMENTS
from athena.runtime.tools import ReadOnlyToolCatalog
from athena.tools.runtime import ToolRuntime

_BIDI_AND_INVISIBLE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]"
)
_EXECUTABLE_PATTERN = re.compile(
    r"(?:\beval\s*\(|\bexec\s*\(|__import__\s*\(|subprocess\.|os\.system\s*\(|"
    r"powershell(?:\.exe)?\b|cmd(?:\.exe)?\s+/c\b|/bin/(?:ba)?sh\b)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?:api[_-]?key|access[_-]?token|authorization|bearer|credential|password|secret)\s*[:=]",
    re.IGNORECASE,
)
_ALLOWED_SUCCESS_CONTRACTS: dict[str, type | tuple[type, ...]] = {
    "requires_root_cause": bool,
    "requires_evidence": bool,
    "required_tool_names": list,
    "max_ticks": int,
    "max_tool_calls": int,
}
_RISK_ORDER = {level.value: index for index, level in enumerate(RiskLevel)}


class CandidateValidationSource(Protocol):
    async def get_trajectory(
        self, tenant_id: str, trajectory_id: str
    ) -> TrajectorySummary | None: ...


class SkillCandidateValidator:
    """Validate immutable Candidate content against Runtime-owned policy facts."""

    def __init__(
        self,
        *,
        tool_specs: Mapping[str, ToolSpecV2] | None = None,
    ) -> None:
        if tool_specs is None:
            catalog = ReadOnlyToolCatalog()
            tool_specs = {
                declaration.name: declaration.as_spec()
                for declaration in catalog.declarations
            }
        self._tool_specs = dict(tool_specs)

    async def validate(
        self,
        candidate: SkillCandidate,
        source: CandidateValidationSource,
    ) -> CandidateValidationReport:
        violations: list[CandidateValidationViolation] = []
        checks: dict[str, bool] = {}

        self._check_schema(candidate, checks, violations)
        self._check_success_contract(candidate, checks, violations)
        self._check_tools(candidate, checks, violations)
        self._check_declared_tool_calls(candidate, checks, violations)
        self._check_text_safety(candidate, checks, violations)
        await self._check_sources(candidate, source, checks, violations)

        schema_valid = not any(
            item.category is CandidateValidationCategory.SCHEMA for item in violations
        )
        security_valid = not any(
            item.category is CandidateValidationCategory.SECURITY for item in violations
        )
        digest = candidate_validation_digest(self._candidate_payload(candidate))
        validated_at = utc_now()
        return CandidateValidationReport(
            report_id=candidate_validation_report_id(
                candidate.tenant_id,
                candidate.candidate_id,
                digest,
            ),
            tenant_id=candidate.tenant_id,
            candidate_id=candidate.candidate_id,
            candidate_digest=digest,
            validator_version=SKILL_CANDIDATE_VALIDATOR_VERSION,
            schema_valid=schema_valid,
            security_valid=security_valid,
            passed=schema_valid and security_valid,
            checks=checks,
            violations=tuple(violations),
            validated_at=validated_at,
        )

    @staticmethod
    def _violation(
        violations: list[CandidateValidationViolation],
        *,
        code: str,
        category: CandidateValidationCategory,
        field: str,
        message: str,
    ) -> None:
        violations.append(
            CandidateValidationViolation(
                code=code,
                category=category,
                field=field,
                message=message,
            )
        )

    def _check_schema(
        self,
        candidate: SkillCandidate,
        checks: dict[str, bool],
        violations: list[CandidateValidationViolation],
    ) -> None:
        version_valid = candidate.schema_version == SKILL_CANDIDATE_SCHEMA_VERSION
        checks["schema_version_supported"] = version_valid
        if not version_valid:
            self._violation(
                violations,
                code="CANDIDATE_SCHEMA_VERSION_UNSUPPORTED",
                category=CandidateValidationCategory.SCHEMA,
                field="schema_version",
                message="Candidate Schema version is not supported.",
            )

        required_text = {
            "candidate_id": candidate.candidate_id,
            "tenant_id": candidate.tenant_id,
            "skill_id": candidate.skill_id,
            "name": candidate.name,
            "description": candidate.description,
            "workflow_type": candidate.workflow_type,
            "environment_type": candidate.environment_type,
            "created_by": candidate.created_by,
        }
        missing = [
            field
            for field, value in required_text.items()
            if not isinstance(value, str) or not value.strip()
        ]
        checks["required_fields_present"] = not missing
        for field in missing:
            self._violation(
                violations,
                code="CANDIDATE_REQUIRED_FIELD_MISSING",
                category=CandidateValidationCategory.SCHEMA,
                field=field,
                message="A required Candidate field is missing.",
            )

        version_number_valid = (
            isinstance(candidate.version, int)
            and not isinstance(candidate.version, bool)
            and candidate.version >= 1
        )
        checks["version_number_valid"] = version_number_valid
        if not version_number_valid:
            self._violation(
                violations,
                code="CANDIDATE_VERSION_INVALID",
                category=CandidateValidationCategory.SCHEMA,
                field="version",
                message="Candidate version must be a positive integer.",
            )

        structures_valid = all(
            (
                isinstance(candidate.manifest, dict) and bool(candidate.manifest),
                isinstance(candidate.trigger, dict) and bool(candidate.trigger),
                isinstance(candidate.procedure, dict)
                and bool(candidate.procedure.get("steps")),
                bool(candidate.allowed_tools),
                bool(candidate.failure_recovery),
                bool(candidate.success_contract),
                bool(candidate.evidence_requirements),
                bool(candidate.source_trajectory_ids),
                bool(candidate.evidence_ids),
            )
        )
        checks["required_structures_present"] = structures_valid
        if not structures_valid:
            self._violation(
                violations,
                code="CANDIDATE_REQUIRED_STRUCTURE_MISSING",
                category=CandidateValidationCategory.SCHEMA,
                field="candidate",
                message="Candidate structured fields are incomplete.",
            )

        budget_valid = (
            isinstance(candidate.token_budget_hint, int)
            and not isinstance(candidate.token_budget_hint, bool)
            and 1 <= candidate.token_budget_hint <= 120_000
        )
        checks["token_budget_valid"] = budget_valid
        if not budget_valid:
            self._violation(
                violations,
                code="CANDIDATE_TOKEN_BUDGET_INVALID",
                category=CandidateValidationCategory.SCHEMA,
                field="token_budget_hint",
                message="Candidate token budget is outside the supported range.",
            )

        manifest_valid = all(
            (
                candidate.manifest.get("schema_version")
                == SKILL_CANDIDATE_SCHEMA_VERSION,
                candidate.manifest.get("candidate_only") is True,
                candidate.manifest.get("creates_tool") is False,
                candidate.manifest.get("readonly") is True,
                candidate.manifest.get("activation_allowed") is False,
            )
        )
        checks["manifest_invariants_hold"] = manifest_valid
        if not manifest_valid:
            self._violation(
                violations,
                code="CANDIDATE_MANIFEST_INVARIANT_FAILED",
                category=CandidateValidationCategory.SECURITY,
                field="manifest",
                message="Candidate-only and read-only manifest invariants must hold.",
            )

        manifest_consistent = all(
            (
                candidate.manifest.get("skill_id") == candidate.skill_id,
                candidate.manifest.get("version") == candidate.version,
                candidate.manifest.get("name") == candidate.name,
                candidate.manifest.get("description") == candidate.description,
                candidate.manifest.get("trigger") == candidate.trigger,
                candidate.manifest.get("allowed_tools")
                == list(candidate.allowed_tools),
                candidate.manifest.get("failure_recovery")
                == list(candidate.failure_recovery),
                candidate.manifest.get("success_contract")
                == candidate.success_contract,
                candidate.manifest.get("evidence_requirements")
                == list(candidate.evidence_requirements),
                candidate.manifest.get("token_budget_hint")
                == candidate.token_budget_hint,
                candidate.manifest.get("source_trajectory_ids")
                == list(candidate.source_trajectory_ids),
                candidate.manifest.get("risk_level") == candidate.risk_level,
            )
        )
        checks["manifest_matches_candidate"] = manifest_consistent
        if not manifest_consistent:
            self._violation(
                violations,
                code="CANDIDATE_MANIFEST_SCHEMA_MISMATCH",
                category=CandidateValidationCategory.SCHEMA,
                field="manifest",
                message="Candidate Manifest does not match persisted Schema fields.",
            )

        steps = candidate.procedure.get("steps", [])
        procedure_valid = (
            isinstance(steps, list)
            and bool(steps)
            and all(isinstance(item, str) and item.strip() for item in steps)
            and candidate.procedure.get("execution_mode")
            == "readonly_recommendation_only"
        )
        checks["procedure_schema_valid"] = procedure_valid
        if not procedure_valid:
            self._violation(
                violations,
                code="CANDIDATE_PROCEDURE_SCHEMA_INVALID",
                category=CandidateValidationCategory.SCHEMA,
                field="procedure",
                message="Candidate Procedure must contain bounded read-only steps.",
            )

        status_valid = candidate.status in {CANDIDATE_STATUS, REJECTED_STATUS}
        checks["candidate_status_non_active"] = (
            status_valid and not candidate.online_eligible
        )
        if not checks["candidate_status_non_active"]:
            self._violation(
                violations,
                code="CANDIDATE_STATUS_FORBIDDEN",
                category=CandidateValidationCategory.SECURITY,
                field="status",
                message="Validation cannot operate on an online-eligible Candidate.",
            )

    def _check_success_contract(
        self,
        candidate: SkillCandidate,
        checks: dict[str, bool],
        violations: list[CandidateValidationViolation],
    ) -> None:
        contract = candidate.success_contract
        valid = isinstance(contract, dict) and bool(contract)
        if valid:
            try:
                json.dumps(contract, ensure_ascii=True, sort_keys=True)
            except (TypeError, ValueError):
                valid = False
        if valid and contract is not None:
            for key, value in contract.items():
                expected = _ALLOWED_SUCCESS_CONTRACTS.get(key)
                if expected is None or not isinstance(value, expected):
                    valid = False
                    break
                if key in {"max_ticks", "max_tool_calls"} and (
                    isinstance(value, bool) or value < 1
                ):
                    valid = False
                    break
            useful = any(
                (
                    contract.get("requires_root_cause") is True,
                    contract.get("requires_evidence") is True,
                    bool(contract.get("required_tool_names")),
                )
            )
            valid = valid and useful
        checks["success_contract_executable"] = valid
        if not valid:
            self._violation(
                violations,
                code="CANDIDATE_SUCCESS_CONTRACT_INVALID",
                category=CandidateValidationCategory.SCHEMA,
                field="success_contract",
                message="Success contract is unsupported or has no deterministic Oracle.",
            )

    def _check_tools(
        self,
        candidate: SkillCandidate,
        checks: dict[str, bool],
        violations: list[CandidateValidationViolation],
    ) -> None:
        unique = len(candidate.allowed_tools) == len(set(candidate.allowed_tools))
        checks["allowed_tools_unique"] = unique
        if not unique:
            self._violation(
                violations,
                code="CANDIDATE_DUPLICATE_TOOL",
                category=CandidateValidationCategory.SCHEMA,
                field="allowed_tools",
                message="Candidate tool names must be unique.",
            )

        known = True
        readonly = True
        capability_valid = True
        risk_valid = (
            candidate.risk_level in _RISK_ORDER and candidate.risk_level == "S1"
        )
        for tool_name in candidate.allowed_tools:
            spec = self._tool_specs.get(tool_name)
            if spec is None:
                known = False
                self._violation(
                    violations,
                    code="CANDIDATE_UNKNOWN_TOOL",
                    category=CandidateValidationCategory.SECURITY,
                    field="allowed_tools",
                    message="Candidate references a tool outside the Runtime registry.",
                )
                continue
            if not spec.readonly:
                readonly = False
                self._violation(
                    violations,
                    code="CANDIDATE_WRITE_TOOL_FORBIDDEN",
                    category=CandidateValidationCategory.SECURITY,
                    field="allowed_tools",
                    message="Write-capable tools are forbidden for this Candidate.",
                )
            if not set(spec.required_capabilities).issubset(candidate.capabilities):
                capability_valid = False
                self._violation(
                    violations,
                    code="CANDIDATE_TOOL_CAPABILITY_FORBIDDEN",
                    category=CandidateValidationCategory.SECURITY,
                    field="capabilities",
                    message="Candidate lacks a capability required by an allowed tool.",
                )
            if (
                candidate.risk_level not in _RISK_ORDER
                or _RISK_ORDER[spec.risk_level.value]
                > _RISK_ORDER[candidate.risk_level]
            ):
                risk_valid = False
                self._violation(
                    violations,
                    code="CANDIDATE_TOOL_RISK_FORBIDDEN",
                    category=CandidateValidationCategory.SECURITY,
                    field="risk_level",
                    message="Candidate risk level is lower than an allowed tool risk.",
                )
        checks["allowed_tools_registered"] = known
        checks["allowed_tools_readonly"] = readonly
        checks["tool_capabilities_authorized"] = capability_valid
        checks["risk_level_authorized"] = risk_valid
        if not risk_valid and not any(
            item.code == "CANDIDATE_TOOL_RISK_FORBIDDEN" for item in violations
        ):
            self._violation(
                violations,
                code="CANDIDATE_RISK_LEVEL_FORBIDDEN",
                category=CandidateValidationCategory.SECURITY,
                field="risk_level",
                message="Only the S1 read-only Candidate risk level is supported.",
            )

    def _check_declared_tool_calls(
        self,
        candidate: SkillCandidate,
        checks: dict[str, bool],
        violations: list[CandidateValidationViolation],
    ) -> None:
        calls: list[object] = []
        for container in (candidate.manifest, candidate.procedure):
            declared = container.get("tool_calls", [])
            if not isinstance(declared, list):
                self._violation(
                    violations,
                    code="CANDIDATE_TOOL_CALL_SCHEMA_INVALID",
                    category=CandidateValidationCategory.SCHEMA,
                    field="tool_calls",
                    message="Declared tool calls must be a list.",
                )
                checks["declared_tool_arguments_valid"] = False
                return
            calls.extend(declared)

        valid = True
        for call in calls:
            if not isinstance(call, dict):
                valid = False
                continue
            tool_name = call.get("tool_name")
            arguments = call.get("arguments")
            if (
                not isinstance(tool_name, str)
                or tool_name not in candidate.allowed_tools
                or not isinstance(arguments, dict)
            ):
                valid = False
                continue
            if set(arguments) & SERVER_CONTROLLED_ARGUMENTS:
                valid = False
                self._violation(
                    violations,
                    code="CANDIDATE_SERVER_ARGUMENT_FORBIDDEN",
                    category=CandidateValidationCategory.SECURITY,
                    field="tool_calls.arguments",
                    message="Candidate declares a Runtime server-controlled argument.",
                )
                continue
            spec = self._tool_specs.get(tool_name)
            if spec is None:
                valid = False
                continue
            error = ToolRuntime.validate_arguments(spec.input_schema, arguments)
            if error is not None:
                valid = False
                self._violation(
                    violations,
                    code=f"CANDIDATE_{error}",
                    category=CandidateValidationCategory.SECURITY,
                    field="tool_calls.arguments",
                    message="Candidate tool arguments violate the Runtime tool Schema.",
                )
        checks["declared_tool_arguments_valid"] = valid
        if not valid and not any(
            item.field == "tool_calls.arguments" for item in violations
        ):
            self._violation(
                violations,
                code="CANDIDATE_TOOL_CALL_SCHEMA_INVALID",
                category=CandidateValidationCategory.SCHEMA,
                field="tool_calls",
                message="Candidate declares an invalid tool call.",
            )

    def _check_text_safety(
        self,
        candidate: SkillCandidate,
        checks: dict[str, bool],
        violations: list[CandidateValidationViolation],
    ) -> None:
        values = tuple(self._text_values(self._candidate_payload(candidate)))
        invisible_clear = not any(_BIDI_AND_INVISIBLE.search(value) for value in values)
        executable_clear = not any(
            _EXECUTABLE_PATTERN.search(value) for value in values
        )
        secret_clear = not any(_SECRET_ASSIGNMENT.search(value) for value in values)
        checks["invisible_control_characters_absent"] = invisible_clear
        checks["executable_instructions_absent"] = executable_clear
        checks["secret_assignments_absent"] = secret_clear
        for passed, code, message in (
            (
                invisible_clear,
                "CANDIDATE_INVISIBLE_CONTROL_CHARACTER",
                "Candidate text contains invisible or bidirectional control characters.",
            ),
            (
                executable_clear,
                "CANDIDATE_EXECUTABLE_INSTRUCTION_FORBIDDEN",
                "Candidate text contains an executable host instruction.",
            ),
            (
                secret_clear,
                "CANDIDATE_SECRET_PATTERN_FORBIDDEN",
                "Candidate text contains a secret assignment pattern.",
            ),
        ):
            if not passed:
                self._violation(
                    violations,
                    code=code,
                    category=CandidateValidationCategory.SECURITY,
                    field="candidate_text",
                    message=message,
                )

    async def _check_sources(
        self,
        candidate: SkillCandidate,
        source: CandidateValidationSource,
        checks: dict[str, bool],
        violations: list[CandidateValidationViolation],
    ) -> None:
        valid = bool(candidate.source_trajectory_ids)
        for trajectory_id in candidate.source_trajectory_ids:
            trajectory = await source.get_trajectory(candidate.tenant_id, trajectory_id)
            if (
                trajectory is None
                or trajectory.tenant_id != candidate.tenant_id
                or trajectory.status is not TrajectoryStatus.ELIGIBLE
                or not trajectory.admission.eligible
                or not all(trajectory.admission.checks.values())
            ):
                valid = False
                self._violation(
                    violations,
                    code="CANDIDATE_SOURCE_TRAJECTORY_INVALID",
                    category=CandidateValidationCategory.SCHEMA,
                    field="source_trajectory_ids",
                    message="A source trajectory is missing or not Eligible.",
                )
        checks["source_trajectories_eligible"] = valid

    @staticmethod
    def _text_values(value: object):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from SkillCandidateValidator._text_values(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from SkillCandidateValidator._text_values(item)

    @staticmethod
    def _candidate_payload(candidate: SkillCandidate) -> dict[str, object]:
        return {
            "schema_version": candidate.schema_version,
            "skill_id": candidate.skill_id,
            "version": candidate.version,
            "name": candidate.name,
            "description": candidate.description,
            "workflow_type": candidate.workflow_type,
            "environment_type": candidate.environment_type,
            "capabilities": list(candidate.capabilities),
            "manifest": candidate.manifest,
            "trigger": candidate.trigger,
            "allowed_tools": list(candidate.allowed_tools),
            "procedure": candidate.procedure,
            "failure_recovery": list(candidate.failure_recovery),
            "success_contract": candidate.success_contract,
            "evidence_requirements": list(candidate.evidence_requirements),
            "token_budget_hint": candidate.token_budget_hint,
            "source_trajectory_ids": list(candidate.source_trajectory_ids),
            "evidence_ids": list(candidate.evidence_ids),
            "risk_level": candidate.risk_level,
        }


__all__ = ["CandidateValidationSource", "SkillCandidateValidator"]
