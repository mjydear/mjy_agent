"""Tests for benchmark reports."""

from __future__ import annotations

import pytest

from athena.agent.base import AgentResponse
from athena.evaluation import BenchmarkCase, BenchmarkEngine, BenchmarkReport


async def runner(query: str) -> AgentResponse:
    return AgentResponse(answer=f"ok {query}", steps=["one"])


@pytest.mark.asyncio
async def test_benchmark_engine_generates_report() -> None:
    engine = BenchmarkEngine(runner)
    results = await engine.run_cases(
        (BenchmarkCase(name="case", query="pod", expected_keywords=("ok",)),)
    )
    report = BenchmarkReport.from_results(results)

    assert report.success_rate == 1.0
    assert "Success Rate" in report.to_markdown()
