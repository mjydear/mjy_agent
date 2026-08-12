"""Run legacy ReAct against declared LIVE Kubernetes benchmark cases."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from pathlib import Path

from athena.agent.policy.contracts import DataOrigin
from athena.cli.main import build_agent
from athena.config import load_settings
from athena.evaluation.live_k8s import (
    LiveBenchmarkArtifactWriter,
    LiveBenchmarkCandidate,
    LiveEvidence,
    LiveK8sBenchmarkRunner,
    LiveK8sCase,
    LiveK8sCaseLoader,
)
from athena.observability.decision_trace import StructuredTraceRecorder
from athena.tools.cloud.k8s import K8sReadOnlyDiagnoser


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/k8s-live/cases"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/live-benchmarks")
    )
    parser.add_argument("--run-id", required=True)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> Path:
    settings = load_settings(args.config)
    if settings.ops.mode != "real":
        raise RuntimeError("LIVE benchmark requires ops.mode=real")
    if settings.ops.kubernetes.fallback_policy != "fail_closed":
        raise RuntimeError(
            "LIVE benchmark requires Kubernetes fallback_policy=fail_closed"
        )
    if settings.ops.prometheus.enabled and (
        settings.ops.prometheus.fallback_policy != "fail_closed"
        or not settings.ops.prometheus.base_url.startswith(("http://", "https://"))
    ):
        raise RuntimeError(
            "LIVE benchmark requires a live Prometheus endpoint with "
            "fallback_policy=fail_closed"
        )

    diagnoser = K8sReadOnlyDiagnoser.from_settings(settings)
    cases = LiveK8sCaseLoader().load(args.cases)

    async def legacy_react(case: LiveK8sCase) -> LiveBenchmarkCandidate:
        agent = build_agent(config_path=args.config)
        recorder = StructuredTraceRecorder()
        agent.trace_recorder = recorder
        response = await agent.run(case.objective)
        report = diagnoser.build_report(case.namespace, include_logs=True)
        if diagnoser.client.last_data_origin != "live":
            raise RuntimeError("LIVE benchmark collector received non-LIVE evidence")

        evidence: list[LiveEvidence] = []
        root_causes: list[str] = []
        for finding_index, finding in enumerate(report.findings, start=1):
            root_causes.extend((finding.symptom, *finding.probable_causes))
            for evidence_index, summary in enumerate(finding.evidence, start=1):
                summary_lower = summary.lower()
                evidence_type = "log" if summary_lower.startswith("log:") else "event"
                evidence.append(
                    LiveEvidence(
                        evidence_id=f"{case.case_id}-{finding_index}-{evidence_index}",
                        evidence_type=evidence_type,
                        source="kubernetes",
                        data_origin=DataOrigin.LIVE,
                        summary=summary,
                    )
                )
        return LiveBenchmarkCandidate(
            answer=response.answer,
            root_causes=tuple(root_causes),
            evidence=tuple(evidence),
            actions=(),
            step_count=len(response.steps),
            trace=tuple(
                asdict(event)
                for event in recorder.events_for(agent.last_trace_run_id or "")
            ),
        )

    results = await LiveK8sBenchmarkRunner(legacy_react).run_cases(cases)
    return LiveBenchmarkArtifactWriter().write(
        args.output,
        run_id=args.run_id,
        environment={
            "mode": settings.ops.mode,
            "k8s_context": settings.ops.kubernetes.context,
            "namespace_allowlist": settings.ops.kubernetes.namespace_allowlist,
            "kubernetes_fallback_policy": settings.ops.kubernetes.fallback_policy,
            "prometheus_enabled": settings.ops.prometheus.enabled,
        },
        results=results,
    )


def main() -> None:
    run_dir = asyncio.run(_run(_arguments()))
    print(run_dir)


if __name__ == "__main__":
    main()
