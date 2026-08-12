"""End-to-end wiring checks for the V1 Runtime assembly."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.config import AthenaSettings, DatabaseSettings


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_sqlite_runtime_backend_survives_application_restart(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'runtime.db'}"
    settings = AthenaSettings(
        database=DatabaseSettings(url=database_url, auto_migrate=True),
    )
    repository = Path(__file__).parent / "fixtures" / "runtime_repo"
    payload = {
        "goal": "诊断价格计算失败并给出只读建议",
        "repository_path": str(repository),
        "profile": "standard",
    }

    with TestClient(create_app(settings)) as client:
        created = _data(client.post("/api/runtime/tasks", json=payload))
        task_id = created["id"]
        assert created["execution"]["backend"] == "sqlite-durable"
        detail = _data(client.post(f"/api/runtime/tasks/{task_id}/run"))
        assert detail["status"] == "succeeded"
        assert detail["execution"]["decision_mode"] == "deterministic-demo"

    with TestClient(create_app(settings)) as restarted:
        tasks = _data(restarted.get("/api/runtime/tasks"))["items"]
        assert any(item["id"] == task_id for item in tasks)
        recovered = _data(restarted.get(f"/api/runtime/tasks/{task_id}"))
        assert recovered["status"] == "succeeded"
        assert recovered["budget"]["consumed_tokens"] > 0
