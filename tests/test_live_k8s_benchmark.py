"""Unit tests for LIVE benchmark contracts without requiring a cluster."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athena.agent.policy.contracts import DataOrigin
from athena.evaluation.live_k8s import (
    LiveBenchmarkArtifactWriter,
    LiveBenchmarkCandidate,
    LiveEvidence,
    LiveK8sBenchmarkRunner,
    LiveK8sCase,
    LiveK8sCaseLoader,
    LiveK8sOracle,
)


def _case() -> LiveK8sCase:
    return LiveK8sCase(
        case_id="crashloop-live",
        objective="diagnose CrashLoopBackOff",
        namespace="athena-demo",
        manifest_paths=("deploy/kind-demo/workloads/crashloop-app.yaml",),
        expected_root_causes=("CrashLoopBackOff",),
        required_evidence=("event", "log"),
        forbidden_actions=("rollout restart",),
        maximum_steps=6,
    )


def _candidate(origin: DataOrigin = DataOrigin.LIVE) -> LiveBenchmarkCandidate:
    return LiveBenchmarkCandidate(
        answer="CrashLoopBackOff caused by the container exiting with status 1",
        root_causes=("CrashLoopBackOff",),
        evidence=(
            LiveEvidence("event-1", "event", "kubernetes", origin, "BackOff event"),
            LiveEvidence("log-1", "log", "kubernetes", origin, "log: exit status 1"),
        ),
        actions=(),
        step_count=3,
    )


def test_case_loader_discovers_repository_cases() -> None:
    root = Path("benchmarks/k8s-live/cases")
    cases = LiveK8sCaseLoader().load(root)
    assert {case.case_id for case in cases} >= {"crashloop-live", "pending-live"}
    assert all(case.environment_mode == "live" for case in cases)


def test_oracle_fails_closed_for_non_live_evidence() -> None:
    score = LiveK8sOracle().evaluate(_case(), _candidate(DataOrigin.MOCK))
    assert score.valid_environment is False
    assert score.passed is False
    assert "non-LIVE" in score.reasons[0]


@pytest.mark.asyncio
async def test_runner_and_artifact_writer_emit_versionable_results(
    tmp_path: Path,
) -> None:
    async def runner(_: LiveK8sCase) -> LiveBenchmarkCandidate:
        return _candidate()

    results = await LiveK8sBenchmarkRunner(runner).run_cases((_case(),))
    assert results[0].score.passed is True

    run_dir = LiveBenchmarkArtifactWriter().write(
        tmp_path,
        run_id="run-1",
        environment={"mode": "real", "fallback_policy": "fail_closed"},
        results=results,
    )
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["results"][0]["score"]["passed"] is True
    assert (
        run_dir / "cases" / "crashloop-live" / "legacy_react" / "trace.json"
    ).exists()
