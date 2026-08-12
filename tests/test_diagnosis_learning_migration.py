from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260809_0007_diagnosis_learning_facts.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("diagnosis_learning_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(connection, function) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        function()


EXPECTED = {
    "diagnosis_outcomes": {
        "columns": {
            "id": ("VARCHAR(80)", False),
            "tenant_id": ("VARCHAR(120)", False),
            "task_id": ("VARCHAR(80)", False),
            "root_cause": ("TEXT", True),
            "supporting_evidence_ids_json": ("JSON", False),
            "remediation_recommendation": ("TEXT", True),
            "confidence": ("FLOAT", False),
            "evidence_sufficient": ("BOOLEAN", False),
            "outcome_hash": ("VARCHAR(128)", False),
            "finalized_at": ("DATETIME", False),
            "created_at": ("DATETIME", False),
        },
        "indexes": {
            "ix_diagnosis_outcomes_tenant_id",
            "ix_diagnosis_outcomes_task_id",
            "ix_diagnosis_outcomes_outcome_hash",
            "ix_diagnosis_outcomes_finalized_at",
        },
        "unique_constraints": {"uq_diagnosis_outcome_task"},
    },
    "operator_feedback": {
        "columns": {
            "id": ("VARCHAR(80)", False),
            "tenant_id": ("VARCHAR(120)", False),
            "task_id": ("VARCHAR(80)", False),
            "outcome_id": ("VARCHAR(80)", False),
            "feedback_type": ("VARCHAR(24)", False),
            "corrected_root_cause": ("TEXT", True),
            "corrected_remediation_recommendation": ("TEXT", True),
            "note": ("TEXT", True),
            "submitted_by": ("VARCHAR(160)", False),
            "idempotency_key": ("VARCHAR(256)", False),
            "request_hash": ("VARCHAR(128)", False),
            "created_at": ("DATETIME", False),
        },
        "indexes": {
            "ix_operator_feedback_tenant_id",
            "ix_operator_feedback_task_id",
            "ix_operator_feedback_outcome_id",
            "ix_operator_feedback_feedback_type",
            "ix_operator_feedback_request_hash",
            "ix_operator_feedback_created_at",
        },
        "unique_constraints": {"uq_operator_feedback_idempotency"},
    },
    "diagnosis_recoveries": {
        "columns": {
            "id": ("VARCHAR(80)", False),
            "tenant_id": ("VARCHAR(120)", False),
            "task_id": ("VARCHAR(80)", False),
            "outcome_id": ("VARCHAR(80)", False),
            "feedback_id": ("VARCHAR(80)", False),
            "observed_at": ("DATETIME", False),
            "summary": ("TEXT", False),
            "created_at": ("DATETIME", False),
        },
        "indexes": {
            "ix_diagnosis_recoveries_tenant_id",
            "ix_diagnosis_recoveries_task_id",
            "ix_diagnosis_recoveries_outcome_id",
            "ix_diagnosis_recoveries_feedback_id",
            "ix_diagnosis_recoveries_observed_at",
        },
        "unique_constraints": {"uq_recovery_feedback"},
    },
    "skill_candidates": {
        "columns": {
            "id": ("VARCHAR(96)", False),
            "tenant_id": ("VARCHAR(120)", False),
            "name": ("VARCHAR(160)", False),
            "workflow_type": ("VARCHAR(80)", False),
            "environment_type": ("VARCHAR(80)", False),
            "capabilities_json": ("JSON", False),
            "manifest_json": ("JSON", False),
            "procedure_json": ("JSON", False),
            "status": ("VARCHAR(32)", False),
            "source_outcome_id": ("VARCHAR(120)", False),
            "source_feedback_id": ("VARCHAR(120)", False),
            "evidence_ids_json": ("JSON", False),
            "source_digest": ("VARCHAR(128)", False),
            "source_summary_json": ("JSON", False),
            "created_by": ("VARCHAR(160)", False),
            "replay_report_id": ("VARCHAR(160)", True),
            "shadow_report_id": ("VARCHAR(160)", True),
            "reviewed_by": ("VARCHAR(160)", True),
            "review_note": ("TEXT", True),
            "created_at": ("DATETIME", False),
            "updated_at": ("DATETIME", False),
            "decided_at": ("DATETIME", True),
        },
        "indexes": {
            "ix_skill_candidates_tenant_id",
            "ix_skill_candidates_workflow_type",
            "ix_skill_candidates_status",
            "ix_skill_candidates_source_outcome_id",
            "ix_skill_candidates_source_feedback_id",
            "ix_skill_candidates_source_digest",
        },
        "unique_constraints": {"uq_skill_candidate_source"},
    },
}


def test_upgrade_creates_all_model_schema_objects() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _run_migration(connection, migration.upgrade)
        inspector = sa.inspect(connection)

        assert set(inspector.get_table_names()) == set(EXPECTED)
        for table_name, expected in EXPECTED.items():
            columns = {
                column["name"]: (str(column["type"]), column["nullable"])
                for column in inspector.get_columns(table_name)
            }
            assert columns == expected["columns"]
            assert {
                index["name"]
                for index in inspector.get_indexes(table_name)
                if not index["unique"]
            } == expected["indexes"]
            assert {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(table_name)
            } == expected["unique_constraints"]


def test_downgrade_removes_all_model_schema_objects() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _run_migration(connection, migration.upgrade)
        _run_migration(connection, migration.downgrade)
        assert sa.inspect(connection).get_table_names() == []
