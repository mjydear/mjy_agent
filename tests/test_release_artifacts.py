"""P5-04 release artifact static contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_declares_api_worker_postgres_redis_and_migration() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"postgres", "redis", "migrate", "athena-api", "athena-worker"}.issubset(
        services
    )
    assert "athena" not in services
    assert services["athena-api"]["build"] == "."
    assert services["athena-worker"]["build"] == "."
    assert services["athena-api"]["command"][0] == "uvicorn"
    assert services["athena-worker"]["command"] == [
        "python",
        "-m",
        "athena.cli.main",
        "worker",
    ]
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert "ATHENA_DATABASE_URL" in services["athena-api"]["environment"]
    assert "ATHENA_ALERT_INTEGRATION_TOKENS" in services["athena-api"]["environment"]


def test_helm_chart_contains_required_release_resources() -> None:
    chart_root = Path("deploy/helm/athena")
    assert (chart_root / "Chart.yaml").exists()
    assert (chart_root / "values.yaml").exists()
    templates = {path.name for path in (chart_root / "templates").glob("*.yaml")}
    assert {
        "api-deployment.yaml",
        "worker-deployment.yaml",
        "migration-job.yaml",
        "service.yaml",
        "serviceaccount.yaml",
        "secret.example.yaml",
        "pdb.yaml",
        "hpa.yaml",
        "networkpolicy.yaml",
        "ingress.yaml",
    }.issubset(templates)

    api_template = (chart_root / "templates" / "api-deployment.yaml").read_text(
        encoding="utf-8"
    )
    worker_template = (chart_root / "templates" / "worker-deployment.yaml").read_text(
        encoding="utf-8"
    )
    migration_template = (chart_root / "templates" / "migration-job.yaml").read_text(
        encoding="utf-8"
    )
    assert "name: athena-api" in api_template
    assert "path: /readyz" in api_template
    assert "name: athena-worker" in worker_template
    assert '["python", "-m", "athena.cli.main", "worker"]' in worker_template
    assert '["alembic", "upgrade", "head"]' in migration_template
    assert "secretRef" in api_template
    assert "secretRef" in worker_template


def test_release_runbook_covers_backup_restore_upgrade_rollback_and_secret_rotation() -> (
    None
):
    runbook = Path("docs/runbooks/release_operations.md").read_text(encoding="utf-8")
    for section in (
        "One-command local demo",
        "Backup",
        "Restore",
        "Upgrade",
        "Rollback",
        "Secret rotation",
    ):
        assert f"## {section}" in runbook
    assert "pg_dump" in runbook
    assert "pg_restore" in runbook
    assert "ATHENA_ALERT_INTEGRATION_TOKENS" in runbook
