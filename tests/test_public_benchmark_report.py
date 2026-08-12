"""Checks for the committed, redacted Provider benchmark projection."""

from __future__ import annotations

import json
from pathlib import Path


PUBLIC_RESULT = (
    Path(__file__).parents[1]
    / "docs"
    / "benchmarks"
    / "results"
    / "deepseek-v4-2026-08-11-public.json"
)


def test_public_provider_result_keeps_call_counts_and_manifest() -> None:
    report = json.loads(PUBLIC_RESULT.read_text(encoding="utf-8"))
    experiments = report["experiments"]

    assert experiments["context_ab"]["provider_calls"] == 16
    assert experiments["runtime_react"]["external_calls"] == 25
    assert experiments["complexity_routing"]["external_calls"] == 12
    assert report["manifest"]["source_artifacts"]
    assert all(
        len(value) == 64
        for value in report["manifest"]["source_artifacts"].values()
    )


def test_public_provider_result_contains_no_credentials_or_raw_payloads() -> None:
    serialized = PUBLIC_RESULT.read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY" not in serialized
    assert "Authorization" not in serialized
    assert '"records": [' not in serialized
    assert "must-not-be-read" not in serialized
