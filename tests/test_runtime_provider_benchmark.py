"""Offline contract tests for the complete Runtime Provider benchmark."""

from __future__ import annotations

import asyncio
from pathlib import Path

from athena.evaluation.runtime_provider_benchmark import (
    DryRunDecisionClient,
    ModelPrice,
    RuntimeBenchmarkCase,
    RuntimeContextStrategy,
    RuntimeModelPair,
    RuntimeProviderBenchmarkRunner,
    _normalize_usage,
    summarize_runtime_cells,
)


REPOSITORY = Path(__file__).parent / "fixtures" / "runtime_repo"


def _case() -> RuntimeBenchmarkCase:
    return RuntimeBenchmarkCase(
        case_id="pricing",
        goal="Read pricing.py and run check_pricing.py before making a recommendation.",
        repository_root=str(REPOSITORY),
        tool_sequence=("read_file_range", "run_test"),
        tool_arguments=(
            {"relative_path": "pricing.py", "start_line": 1, "end_line": 80},
            {"relative_path": "check_pricing.py"},
        ),
        expected_tool_names=("read_file_range", "run_test"),
    )


def test_runner_executes_real_multitick_runtime_without_network() -> None:
    pair = RuntimeModelPair(
        light_model="dry-light",
        heavy_model="dry-heavy",
        light_client=DryRunDecisionClient(),
        heavy_client=DryRunDecisionClient(),
    )
    cells = asyncio.run(
        RuntimeProviderBenchmarkRunner(
            prices={
                "dry-light": ModelPrice(input_per_million=1, output_per_million=2),
                "dry-heavy": ModelPrice(input_per_million=3, output_per_million=4),
            }
        ).run(
            model_pairs=(pair,),
            cases=(_case(),),
            strategies=(RuntimeContextStrategy.FULL_HISTORY, RuntimeContextStrategy.FOUR_LAYER),
        )
    )

    assert len(cells) == 2
    assert {cell.cell_id for cell in cells} == {
        "pricing:full_history:light_dry-light_heavy_dry-heavy",
        "pricing:four_layer:light_dry-light_heavy_dry-heavy",
    }
    for cell in cells:
        assert cell.success is True
        assert cell.tick_count == 3
        assert cell.collected_tools == ("read_file_range", "run_test")
        assert cell.evidence_retained is True
        assert len(cell.rounds) == 3
        assert all(round_item.total_tokens > 0 for round_item in cell.rounds)
        assert all(round_item.calls for round_item in cell.rounds)
        assert cell.cost_usd is not None


def test_usage_normalizer_reads_nested_cache_and_reasoning_fields() -> None:
    usage = _normalize_usage(
        {
            "input_tokens": 100,
            "output_tokens": 40,
            "input_tokens_details": {"cached_tokens": 25},
            "output_tokens_details": {"reasoning_tokens": 12},
            "total_tokens": 140,
        }
    )

    assert usage == {
        "input_tokens": 100,
        "output_tokens": 40,
        "cached_tokens": 25,
        "reasoning_tokens": 12,
        "total_tokens": 140,
    }


def test_summary_keeps_strategy_and_model_pair_identity() -> None:
    pair = RuntimeModelPair(
        light_model="dry-light",
        heavy_model="dry-heavy",
        light_client=DryRunDecisionClient(),
        heavy_client=DryRunDecisionClient(),
    )
    cells = asyncio.run(
        RuntimeProviderBenchmarkRunner().run(
            model_pairs=(pair,),
            cases=(_case(),),
            strategies=(RuntimeContextStrategy.FOUR_LAYER,),
        )
    )

    summary = summarize_runtime_cells(cells)
    assert summary[0]["model"] == "light=dry-light;heavy=dry-heavy"
    assert summary[0]["strategy"] == "four_layer"
    assert summary[0]["success_rate"] == 1.0
    assert summary[0]["evidence_retention_rate"] == 1.0
