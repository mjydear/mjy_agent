# Athena Agent Runtime V1 Delivery Plan

## 1. V1 Outcome

P0 proved a runnable, inspectable code-diagnosis loop. V1 turns that loop into
a recoverable Agent Runtime with a real model adapter, governed tools,
four-layer memory, and an evaluated Skill-learning lifecycle.

```text
User goal
-> durable AgentTask + Checkpoint
-> worker lease
-> ContextSnapshot + memory retrieval
-> model routing + structured Decision
-> governed ToolRuntime invocation
-> Tick/Event/Evidence/Usage commit
-> Skill candidate evaluation after verified success
```

V1 is still a single coordinator by default. Runtime-level multi-agent fan-out
is a later capability, enabled only after the coordinator can recover a task
and account for every tool effect.

## 2. P0 Baseline And V1 Boundary

P0 already provides:

- A runnable Runtime Console and `/api/runtime/tasks` contract.
- Bounded `AgentRuntime.advance()` ticks and public events.
- Repository-scoped read-only tools, Artifact, Evidence, Context, and Usage.
- A deterministic no-key diagnosis adapter for tests and demonstrations.

V1 adds:

- Durable execution records and restart recovery.
- An LLM-backed decision adapter with deterministic fallback.
- Token-aware four-layer memory retrieval and compaction.
- Tool effect journaling and idempotent recovery.
- Skill Candidate generation, replay/shadow evaluation, and human-gated promotion.

V1 does not add arbitrary shell execution, automatic production deployment,
or automatic Skill activation.

## 3. Shared Runtime Contract

Every workstream must preserve these seams:

```python
AgentRuntime.advance(task_id, lease_id, resume_input=None) -> AdvanceResult
RuntimeStore.claim(task_id, lease_id) -> AgentTask
RuntimeStore.commit_tick(...) -> None
DecisionEngine.decide(context) -> Decision
ToolGateway.invoke(call, context) -> ToolResult
MemoryLayer.compile(task, checkpoint, evidence, budget) -> ContextSnapshot
SkillLifecycle.observe_completed_task(snapshot) -> SkillCandidate | None
```

Rules:

- One `advance()` commits at most one structured Decision and one logical
  action. There is no raw chain-of-thought in storage, events, or HTTP output.
- Checkpoint, TickEvent, Evidence reference, Usage entry, and EffectJournal
  state use one aggregate commit boundary.
- The server derives repository root, task id, permission scope, tenant, call
  id, and lease id. A model cannot provide them as tool arguments.
- A Skill enters `CANDIDATE`, `REPLAY_PENDING`, `SHADOW_PENDING`, then requires
  a human review gate; no successful task can auto-activate a Skill.

## 4. Four Parallel Workstreams

### A. Durable Execution Plane

Owns `athena/runtime/durable/`, the SQLAlchemy persistence adapter, migration,
checkpoint transaction tests, and worker-facing lease operations.

- Tables: `agent_tasks`, `runtime_checkpoints`, `runtime_tick_events`,
  `runtime_artifacts`, `runtime_evidence`, `runtime_usage`,
  `runtime_tool_effects`.
- Add lease expiration, replay-safe event ordering, and task-level idempotency.
- Keep `InMemoryRuntimeStore` as an explicit local Demo adapter.

Acceptance: restart after a committed tool Tick, resume from the Checkpoint,
and do not invoke that same tool effect a second time.

### B. Model And Tool Governance Plane

Owns `athena/runtime/llm_engine.py`, runtime tool gateway adapters, and focused
model/tool tests.

- Use existing `ModelRouter`, managed model configuration, and `ToolRuntime`.
- Classify task complexity without an extra model call; select ECONOMY or
  QUALITY tier according to budget mode and decision purpose.
- Validate strict Decision JSON; retry format repair once; then use the
  deterministic fallback or ask the operator.
- Select at most three full tool schemas per Tick. Persist EffectJournal state
  before dispatching an effect.

Acceptance: malformed model output cannot execute a tool; forbidden paths and
capabilities remain rejected; routing reason and actual usage are recorded.

### C. Memory And Token Governance Plane

Owns `athena/runtime/memory/`, memory retrieval adapters, compaction reducers,
and focused memory/token tests.

Four layers:

1. **Working Memory**: durable Checkpoint with plan, pending items, and active
   tool-call/result pairs.
2. **Running Summary**: structured compressed history, not free-form transcript.
3. **Evidence Memory**: source-backed facts and Artifact references for the
   current task.
4. **Skill Memory**: evaluated Skill retrieval only; no unreviewed task output
   is injected as a Skill.

Budget policy: reserve output and safety margin first; prepare a summary at
75% input capacity; compact at 90%; use `NORMAL`, `ECONOMY`, `CONVERGE`, then
`FINALIZE` based on task-level budget consumption.

Acceptance: a long task retains goal, constraints, pinned Evidence, unresolved
tool pairs, and pending plan after compaction; raw Artifacts never enter a
model prompt by default.

### D. Skill Learning And Evaluation Plane

Owns `athena/runtime/learning/`, candidate/replay/shadow policy adapters, and
focused learning lifecycle tests. It reuses existing `SkillCandidate` domain
objects where compatible instead of creating a second lifecycle.

- Observe only successful, evidence-backed tasks that have operator feedback.
- Generate a redacted Candidate manifest and procedure with source references.
- Evaluate against fixed replay fixtures, then run shadow mode with no effect.
- Expose review decisions but never auto-promote to `ACTIVE`.

Acceptance: a failed replay or missing Evidence blocks promotion; audit events
show source Task, Evidence, evaluation reports, reviewer, and version.

## 5. Mainline Integration

After the four modules are reviewed, mainline work will:

1. Register the durable store when a database is configured and retain the
   in-memory adapter in Demo profile.
2. Run a bounded worker loop that owns lease acquisition and calls only
   `AgentRuntime.advance()`.
3. Extend Runtime API and Console with recovery state, selected model, context
   budget, tool effect state, memory references, and Skill evaluation history.
4. Keep all existing public P0 endpoints stable.

## 6. Delivery Milestones

| Milestone | Scope | Completion Signal |
|---|---|---|
| M1 | Durable Store + Lease | restart recovery integration test passes |
| M2 | Real Model + Tool Gateway | strict Decision and governed tool tests pass |
| M3 | Four-Layer Memory | compaction and retrieval retention tests pass |
| M4 | Skill Evaluation | candidate replay/shadow gating tests pass |
| M5 | Integrated Console | browser/API task recovery workflow is visible |
| M6 | Multi-Agent Coordinator | only after M1-M5 pass; fan-out has child budgets and cancellation propagation |

## 7. V1 Quality Gate

- A task resumes after a process restart without replaying a completed effect.
- Provider unavailability falls back safely without exposing secrets or hidden
  reasoning.
- Token reservation, model tier, routing reason, and actual usage are visible.
- Context compaction preserves the required anchors and evidence references.
- A Skill cannot become active without replay, shadow, and human review.
- Runtime Console renders active/recovered/waiting/cancelled terminal states
  from the public API at desktop and mobile widths.

## 8. 主线实现状态

V1 主线已经完成装配：

- 默认 Demo 使用 `FourLayerRuntimeContextCompiler`，没有 API Key 也能跑通完整只读 ReAct。
- 配置模型后使用 `LLMDecisionEngine`，严格 JSON、一次格式修复、预算偏好和复杂度路由都会写入 Usage。
- 配置 SQLite 且开启自动迁移后使用 `DurableRuntimeStore`，Runtime Worker 负责租约和单 Tick 执行。
- Runtime 专用 Effect Journal 已覆盖工具执行崩溃窗口，已完成的只读结果会被恢复复用。
- Runtime Learning API 已提供 Candidate、Replay、Shadow、Review、Handoff；Handoff 永远是人工交接包，`activation_allowed=false`。
- Runtime Console 的运行面板会显示执行后端、决策模式、记忆策略、预算和 Token 消耗。

详细中文复习稿见 [Agent Runtime V1 实现说明](docs/architecture/agent-runtime-v1-implementation.md)。
