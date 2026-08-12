"""Demo 2 - GEPA self-evolution from trace to reusable Skill."""

from __future__ import annotations

import asyncio

from athena.learning.complexity import ComplexityEvaluator
from athena.learning.skill_gen import SkillGenerator
from athena.learning.tracer import TraceEvent
from athena.memory.skill import SkillLibrary


async def main() -> None:
    """Run the self-evolution demo."""
    run_id = "demo2-run-001"
    events = (
        TraceEvent(
            "agent.step",
            run_id,
            payload={"detail": "Collect pod status and recent events"},
        ),
        TraceEvent("tool.call", run_id, payload={"tool": "k8s_cluster_snapshot"}),
        TraceEvent(
            "agent.step",
            run_id,
            payload={"detail": "Inspect CrashLoopBackOff container logs"},
        ),
        TraceEvent("tool.call", run_id, payload={"tool": "k8s_logs"}),
        TraceEvent(
            "agent.step",
            run_id,
            payload={"detail": "Recommend rollback and resource limit check"},
        ),
    )

    complexity = ComplexityEvaluator(skill_threshold=0.45).evaluate(
        events, task_difficulty=0.8
    )
    generated = SkillGenerator().build_skill(
        "Diagnose K8s CrashLoop", events, complexity, success=True
    )
    library = SkillLibrary()
    await library.add_skill(generated.skill)
    matches = await library.match("pod keeps restarting with CrashLoopBackOff", top_k=1)

    print("# Demo 2: Self Evolution")
    print(
        f"Complexity score: {complexity.score:.2f}, should_generate_skill={complexity.should_generate_skill}"
    )
    print(
        f"Generated skill: {generated.skill.name}, confidence={generated.confidence:.2f}"
    )
    print("\n## Skill Content")
    print(generated.skill.content)
    print("\n## Next-time Recall")
    for skill in matches:
        print(f"- {skill.name}: {skill.description}")


if __name__ == "__main__":
    asyncio.run(main())
