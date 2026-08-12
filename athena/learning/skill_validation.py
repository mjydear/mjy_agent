"""Deterministic static and security validation for offline Skill Candidates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

SKILL_CANDIDATE_SCHEMA_VERSION = "athena.skill-candidate.v1"
SKILL_CANDIDATE_VALIDATOR_VERSION = "athena.skill-candidate-validator.v1"


class CandidateValidationCategory(StrEnum):
    SCHEMA = "schema"
    SECURITY = "security"


@dataclass(frozen=True)
class CandidateValidationViolation:
    """One stable, safe-to-persist validation failure."""

    code: str
    category: CandidateValidationCategory
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "category": self.category.value,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True)
class CandidateValidationReport:
    """Auditable result of all Candidate checks; never activates a Skill."""

    report_id: str
    tenant_id: str
    candidate_id: str
    candidate_digest: str
    validator_version: str
    schema_valid: bool
    security_valid: bool
    passed: bool
    checks: dict[str, bool]
    violations: tuple[CandidateValidationViolation, ...]
    validated_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "tenant_id": self.tenant_id,
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "validator_version": self.validator_version,
            "schema_valid": self.schema_valid,
            "security_valid": self.security_valid,
            "passed": self.passed,
            "checks": dict(self.checks),
            "violations": [item.to_dict() for item in self.violations],
            "validated_at": self.validated_at.isoformat(),
            "activation_allowed": False,
        }


def candidate_validation_digest(payload: dict[str, object]) -> str:
    """Return a stable digest without serializing audit timestamps."""

    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def candidate_validation_report_id(
    tenant_id: str,
    candidate_id: str,
    candidate_digest: str,
    validator_version: str = SKILL_CANDIDATE_VALIDATOR_VERSION,
) -> str:
    encoded = json.dumps(
        {
            "tenant_id": tenant_id,
            "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "validator_version": validator_version,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"candidate-validation-{hashlib.sha256(encoded).hexdigest()[:32]}"


__all__ = [
    "CandidateValidationCategory",
    "CandidateValidationReport",
    "CandidateValidationViolation",
    "SKILL_CANDIDATE_SCHEMA_VERSION",
    "SKILL_CANDIDATE_VALIDATOR_VERSION",
    "candidate_validation_digest",
    "candidate_validation_report_id",
]
