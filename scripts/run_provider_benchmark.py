"""Run real Provider A/B experiments; dry-run is the default."""

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

from athena.evaluation.provider_benchmark import (
    ContextStrategy,
    ModelPrice,
    ProviderBenchmarkCase,
    ProviderBenchmarkRunner,
    summarize_records,
)

DEFAULT_CASES = PROJECT_ROOT / "benchmarks" / "agent-runtime" / "provider-cases.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "artifacts" / "provider-benchmarks" / "latest.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
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
        choices=[item.value for item in ContextStrategy],
        default=None,
    )
    parser.add_argument("--live", action="store_true", help="allow real Provider calls")
    return parser


def _load_cases(path: Path) -> tuple[ProviderBenchmarkCase, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("benchmark cases must be a JSON array")
    return tuple(
        ProviderBenchmarkCase(
            **{
                **item,
                "history": tuple(item["history"]),
                "summary": tuple(item["summary"]),
                "evidence": tuple(item["evidence"]),
                "expected_evidence_ids": tuple(item["expected_evidence_ids"]),
            }
        )
        for item in raw
    )


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


async def _run_live(args: argparse.Namespace, cases: tuple[ProviderBenchmarkCase, ...]) -> dict[str, Any]:
    if not args.light_model or not args.heavy_model:
        raise SystemExit("--live requires --light-model and --heavy-model")
    from athena.infra.llm import LLMClientFactory

    model_roles = {"light": args.light_model, "heavy": args.heavy_model}
    max_tokens_by_role = {
        "light": args.light_max_tokens,
        "heavy": args.heavy_max_tokens,
    }
    clients = {
        model: LLMClientFactory.create(
            provider=args.provider,
            model=model,
            temperature=0.0,
            max_tokens=max_tokens_by_role[role],
        )
        for role, model in model_roles.items()
    }
    strategies = tuple(
        ContextStrategy(item)
        for item in (args.strategies or [item.value for item in ContextStrategy])
    )
    records = await ProviderBenchmarkRunner(
        prices=_load_prices(args.price_config)
    ).run(clients=clients, cases=cases, strategies=strategies)
    return {
        "schema_version": "provider-benchmark.v1",
        "live": True,
        "provider": args.provider,
        "models": model_roles,
        "case_count": len(cases),
        "cell_count": len(records),
        "summary": summarize_records(records),
        "records": [record.to_dict() for record in records],
    }


def main() -> None:
    args = _parser().parse_args()
    cases = _load_cases(args.cases)
    if not args.live:
        strategies = args.strategies or [item.value for item in ContextStrategy]
        cells = 2 * len(cases) * len(strategies)
        print(
            json.dumps(
                {
                    "live": False,
                    "case_count": len(cases),
                    "planned_cells": cells,
                    "external_calls": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    report = asyncio.run(_run_live(args, cases))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
