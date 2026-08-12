"""Offline tests for the provider benchmark seam."""

from __future__ import annotations

import asyncio
import json

from athena.evaluation.provider_benchmark import (
    ContextStrategy,
    ModelPrice,
    ProviderBenchmarkCase,
    ProviderBenchmarkRunner,
    build_messages,
    summarize_records,
)
from athena.infra.llm import LLMResponse


class _FakeClient:
    async def complete(self, messages):
        payload = json.loads(messages[-1].content)
        evidence = payload.get("evidence", payload.get("evidence_memory", []))
        return LLMResponse(
            content=json.dumps(
                {
                    "answer": "verified",
                    "evidence_ids": [item["evidence_id"] for item in evidence],
                }
            ),
            model="fake",
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )


class _MalformedClient:
    async def complete(self, messages):
        return LLMResponse(
            content="not-json",
            model="fake",
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )


def _case() -> ProviderBenchmarkCase:
    return ProviderBenchmarkCase(
        case_id="case-1",
        goal="diagnose",
        history=("old", "recent"),
        summary=("fact",),
        evidence=({"evidence_id": "e1", "artifact_id": "a1", "summary": "fact"},),
        expected_evidence_ids=("e1",),
        artifact_lines=10,
    )


def test_four_layer_context_never_contains_artifact_body() -> None:
    messages = build_messages(_case(), ContextStrategy.FOUR_LAYER)
    assert "irrelevant provider output" not in messages[-1].content
    assert "artifact-pricing" not in messages[-1].content


def test_runner_records_usage_and_cost_without_network() -> None:
    records = asyncio.run(
        ProviderBenchmarkRunner(
            prices={"fake": ModelPrice(input_per_million=1.0, output_per_million=2.0)}
        ).run(
            clients={"fake": _FakeClient()},
            cases=(_case(),),
            strategies=(ContextStrategy.FOUR_LAYER,),
        )
    )
    assert len(records) == 1
    assert records[0].status == "succeeded"
    assert records[0].input_tokens == 100
    assert records[0].total_tokens == 120
    assert records[0].cost_usd == 0.00014


def test_all_context_strategies_are_explicit() -> None:
    for strategy in ContextStrategy:
        messages = build_messages(_case(), strategy)
        assert messages[0].role == "system"
        assert messages[1].role == "user"


def test_malformed_response_keeps_provider_usage_as_quality_failure() -> None:
    records = asyncio.run(
        ProviderBenchmarkRunner(
            prices={"fake": ModelPrice(input_per_million=1.0, output_per_million=2.0)}
        ).run(
            clients={"fake": _MalformedClient()},
            cases=(_case(),),
            strategies=(ContextStrategy.FOUR_LAYER,),
        )
    )

    record = records[0]
    assert record.status == "quality_failed"
    assert record.error == "JSONDecodeError"
    assert record.input_tokens == 100
    assert record.output_tokens == 20
    assert record.total_tokens == 120
    assert record.cost_usd == 0.00014


def test_summary_groups_by_actual_model_and_strategy() -> None:
    records = asyncio.run(
        ProviderBenchmarkRunner().run(
            clients={"gpt-test": _FakeClient()},
            cases=(_case(),),
            strategies=(ContextStrategy.FOUR_LAYER,),
        )
    )
    summary = summarize_records(records)
    assert summary[0]["model"] == "gpt-test"
    assert summary[0]["strategy"] == "four_layer"
    assert summary[0]["success_rate"] == 1.0
