"""Run the multi-tick Agent Runtime Provider benchmark.

Default mode executes a complete deterministic Runtime workflow locally.
Network calls are possible only with ``--live`` plus explicit model IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=False)

from athena.evaluation.runtime_provider_benchmark import (
    DryRunDecisionClient,
    ModelPrice,
    RuntimeBenchmarkCase,
    RuntimeContextStrategy,
    RuntimeModelPair,
    RuntimeProviderBenchmarkRunner,
    summarize_runtime_cells,
)
from athena.runtime.models import TaskProfile


DEFAULT_CASES = PROJECT_ROOT / "benchmarks" / "agent-runtime" / "runtime-provider-cases.json"
DEFAULT_REPOSITORY = PROJECT_ROOT / "tests" / "fixtures" / "runtime_repo"
DEFAULT_OUTPUT = PROJECT_ROOT / "artifacts" / "provider-benchmarks" / "runtime-latest.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--repository-root", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--light-model", default=None)
    parser.add_argument("--heavy-model", default=None)
    parser.add_argument("--light-max-tokens", type=int, default=512)
    parser.add_argument("--heavy-max-tokens", type=int, default=1024)
    parser.add_argument("--provider", default="litellm")
    parser.add_argument("--price-config", type=Path, default=None)
    parser.add_argument(
        "--strategies",
        nargs="*",
        choices=[item.value for item in RuntimeContextStrategy],
        default=None,
    )
    parser.add_argument("--live", action="store_true", help="allow real Provider calls")
    return parser


def _load_cases(path: Path, repository_root: Path) -> tuple[RuntimeBenchmarkCase, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("benchmark cases must be a JSON array")
    cases: list[RuntimeBenchmarkCase] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each benchmark case must be an object")
        configured_root = item.get("repository_root")
        root = Path(configured_root) if isinstance(configured_root, str) else repository_root
        if not root.is_absolute():
            root = (PROJECT_ROOT / root).resolve()
        cases.append(
            RuntimeBenchmarkCase(
                case_id=str(item["case_id"]),
                goal=str(item["goal"]),
                repository_root=str(root),
                tool_sequence=tuple(str(value) for value in item["tool_sequence"]),
                tool_arguments=tuple(dict(value) for value in item["tool_arguments"]),
                expected_tool_names=tuple(str(value) for value in item["expected_tool_names"]),
                profile=TaskProfile(str(item.get("profile", "standard"))),
                max_ticks=(int(item["max_ticks"]) if item.get("max_ticks") is not None else None),
            )
        )
    return tuple(cases)


def _load_prices(path: Path | None) -> dict[str, ModelPrice]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("price config must be a JSON object keyed by model")
    return {
        str(model): ModelPrice(**value)
        for model, value in raw.items()
        if not str(model).startswith("_")
    }


def _strategies(args: argparse.Namespace) -> tuple[RuntimeContextStrategy, ...]:
    values = args.strategies or [item.value for item in RuntimeContextStrategy]
    return tuple(RuntimeContextStrategy(value) for value in values)


def _dry_pair() -> RuntimeModelPair:
    return RuntimeModelPair(
        light_model="dry-run-light",
        heavy_model="dry-run-heavy",
        light_client=DryRunDecisionClient(),
        heavy_client=DryRunDecisionClient(),
    )


def _live_pair(args: argparse.Namespace) -> RuntimeModelPair:
    if not args.light_model or not args.heavy_model:
        raise SystemExit("--live requires --light-model and --heavy-model")
    from athena.infra.llm import LLMClientFactory

    light = LLMClientFactory.create(
        provider=args.provider,
        model=args.light_model,
        temperature=0.0,
        max_tokens=args.light_max_tokens,
    )
    heavy = light if args.heavy_model == args.light_model else LLMClientFactory.create(
        provider=args.provider,
        model=args.heavy_model,
        temperature=0.0,
        max_tokens=args.heavy_max_tokens,
    )
    return RuntimeModelPair(
        light_model=args.light_model,
        heavy_model=args.heavy_model,
        light_client=light,
        heavy_client=heavy,
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    cases = _load_cases(args.cases, args.repository_root)
    pair = _live_pair(args) if args.live else _dry_pair()
    cells = await RuntimeProviderBenchmarkRunner(
        prices=_load_prices(args.price_config)
    ).run(model_pairs=(pair,), cases=cases, strategies=_strategies(args))
    return {
        "schema_version": "runtime-provider-benchmark.v1",
        "live": args.live,
        "provider": args.provider if args.live else "offline-deterministic",
        "models": {
            "light": pair.light_model,
            "heavy": pair.heavy_model,
        },
        "case_count": len(cases),
        "cell_count": len(cells),
        "external_calls": sum(
            len(round_item.calls)
            for cell in cells
            for round_item in cell.rounds
        ) if args.live else 0,
        "summary": summarize_runtime_cells(cells),
        "cells": [cell.to_dict() for cell in cells],
    }


def main() -> None:
    args = _parser().parse_args()
    report = asyncio.run(_run(args))
    if args.live:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
