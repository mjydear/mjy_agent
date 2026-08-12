"""Runtime Skill learning observes evidence but never auto-activates a Skill."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from athena.runtime import (
    AgentRuntime,
    AgentTask,
    InMemoryRuntimeStore,
    TaskStatus,
)
from athena.runtime.learning import (
    CANDIDATE_STATUS,
    REJECTED_STATUS,
    REVIEW_PENDING_STATUS,
    SHADOW_STATUS,
    OperatorFeedback,
    ReplayCase,
    ReviewGate,
    RuntimeSkillLearningError,
    RuntimeSkillLearningLifecycle,
    RuntimeSkillLearningObserver,
    RuntimeSkillReplayEvaluator,
    RuntimeSkillShadowEvaluator,
    ShadowCase,
)


def _completed_snapshot():
    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal="Diagnose the pricing calculation failure",
        repository_root=str(Path(__file__).parent / "fixtures" / "runtime_repo"),
    )
    store.create_task(task)
    runtime = AgentRuntime(store=store)
    for _ in range(4):
        runtime.advance(task.task_id, lease_id="worker-a")
    snapshot = store.snapshot(task.task_id)
    assert snapshot.task.status is TaskStatus.SUCCEEDED
    return snapshot


def _feedback() -> OperatorFeedback:
    return OperatorFeedback(
        feedback_id="feedback-runtime-1",
        accepted=True,
        verified=True,
        summary="Operator confirmed recovery. api_key=never-copy hidden thought: never-copy.",
        submitted_by="reviewer-a",
    )


def test_observer_requires_completed_evidence_backed_task_and_verified_feedback() -> None:
    snapshot = _completed_snapshot()
    observer = RuntimeSkillLearningObserver(min_evidence=3)

    missing_feedback = observer.observe_completed_task(snapshot, None)
    assert missing_feedback.candidate is None
    assert missing_feedback.blocked_reason == "VERIFIED_OPERATOR_FEEDBACK_REQUIRED"

    unsafe_evidence = replace(
        snapshot.evidence[0],
        summary="API_KEY=raw-evidence-secret hidden thought: raw evidence details",
    )
    redacted_snapshot = replace(
        snapshot,
        evidence=(unsafe_evidence, *snapshot.evidence[1:]),
    )
    result = observer.observe_completed_task(redacted_snapshot, _feedback())
    candidate = result.candidate
    assert candidate is not None
    assert candidate.status == CANDIDATE_STATUS
    assert candidate.online_eligible is False
    assert candidate.manifest["candidate_only"] is True
    assert candidate.manifest["creates_tool"] is False
    assert candidate.manifest["activation_allowed"] is False
    assert len(candidate.source_evidence_ids) == 3
    serialized = repr(candidate)
    assert "never-copy" not in serialized
    assert "raw-evidence-secret" not in serialized
    assert "api_key=" not in serialized.lower()
    assert "hidden thought" not in serialized.lower()
    assert "artifact_id" not in candidate.source_summary
    assert not hasattr(candidate, "artifacts")


def test_evidence_threshold_and_final_report_references_are_gates() -> None:
    snapshot = _completed_snapshot()
    observer = RuntimeSkillLearningObserver(min_evidence=4)

    result = observer.observe_completed_task(snapshot, _feedback())
    assert result.candidate is None
    assert result.blocked_reason == "EVIDENCE_THRESHOLD_NOT_MET"
    assert result.details == {"minimum": 4, "observed": 3}


def test_replay_shadow_and_human_review_require_the_full_governed_path() -> None:
    snapshot = _completed_snapshot()
    candidate = RuntimeSkillLearningObserver(min_evidence=3).observe_completed_task(
        snapshot, _feedback()
    ).candidate
    assert candidate is not None
    lifecycle = RuntimeSkillLearningLifecycle()

    replay_pending = lifecycle.mark_replay_pending(candidate)
    replay = RuntimeSkillReplayEvaluator().evaluate(
        replay_pending,
        (
            ReplayCase(
                case_id="pricing-failure",
                expected_root_cause=str(candidate.procedure["root_cause"]),
                required_evidence_ids=candidate.source_evidence_ids,
            ),
        ),
    )
    shadow_pending = lifecycle.record_replay(replay_pending, replay)
    assert shadow_pending.status == SHADOW_STATUS

    shadow = RuntimeSkillShadowEvaluator().evaluate(
        shadow_pending,
        (
            ShadowCase(
                case_id="pricing-shadow",
                observed_root_cause=str(candidate.procedure["root_cause"]),
                observed_evidence_ids=(candidate.source_evidence_ids[0],),
                effect_count=0,
            ),
        ),
    )
    review_pending = lifecycle.record_shadow(shadow_pending, shadow)
    assert review_pending.status == REVIEW_PENDING_STATUS
    assert review_pending.handoff_ready is False

    reviewed = lifecycle.review(
        review_pending,
        ReviewGate(reviewer="lead-a", approved=True, note="readonly scope reviewed"),
    )
    assert reviewed.status == REVIEW_PENDING_STATUS
    assert reviewed.handoff_ready is True
    handoff = lifecycle.handoff(reviewed)
    assert handoff.activation_allowed is False
    assert handoff.requires_manual_draft_creation is True
    assert handoff.audit["action"] == "manual_human_draft_creation_required"


def test_failed_replay_or_shadow_rejects_candidate_and_effectful_shadow_cannot_pass() -> None:
    snapshot = _completed_snapshot()
    candidate = RuntimeSkillLearningObserver(min_evidence=3).observe_completed_task(
        snapshot, _feedback()
    ).candidate
    assert candidate is not None
    lifecycle = RuntimeSkillLearningLifecycle()
    replay_pending = lifecycle.mark_replay_pending(candidate)
    failed_replay = RuntimeSkillReplayEvaluator().evaluate(
        replay_pending,
        (
            ReplayCase(
                case_id="wrong-root-cause",
                expected_root_cause="not the observed cause",
                required_evidence_ids=candidate.source_evidence_ids,
            ),
        ),
    )
    rejected = lifecycle.record_replay(replay_pending, failed_replay)
    assert rejected.status == REJECTED_STATUS
    assert rejected.online_eligible is False

    replay_pending = lifecycle.mark_replay_pending(candidate)
    passing_replay = RuntimeSkillReplayEvaluator().evaluate(
        replay_pending,
        (
            ReplayCase(
                case_id="passing-replay",
                expected_root_cause=str(candidate.procedure["root_cause"]),
                required_evidence_ids=candidate.source_evidence_ids,
            ),
        ),
    )
    shadow_pending = lifecycle.record_replay(replay_pending, passing_replay)
    effectful_shadow = RuntimeSkillShadowEvaluator().evaluate(
        shadow_pending,
        (
            ShadowCase(
                case_id="effectful-shadow",
                observed_root_cause=str(candidate.procedure["root_cause"]),
                observed_evidence_ids=(candidate.source_evidence_ids[0],),
                effect_count=1,
            ),
        ),
    )
    assert effectful_shadow.passed is False
    assert effectful_shadow.results[0].reason_code == "SHADOW_EFFECT_DETECTED"
    rejected_shadow = lifecycle.record_shadow(shadow_pending, effectful_shadow)
    assert rejected_shadow.status == REJECTED_STATUS

    try:
        lifecycle.handoff(rejected_shadow)
    except RuntimeSkillLearningError as error:
        assert error.error_code == "SKILL_CANDIDATE_NOT_HUMAN_APPROVED"
    else:
        raise AssertionError("rejected candidates must not produce a handoff")
