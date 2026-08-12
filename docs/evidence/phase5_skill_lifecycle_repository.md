# Phase 5 Skill Lifecycle Repository Evidence

Date: 2026-07-19

## Scope

This evidence covers P5-02 Skill definition/version repository and lifecycle.

## Implementation facts

- Added durable `skill_definitions` and `skill_versions` models.
- Added Alembic revision `20260719_0006_skill_lifecycle.py`.
- Added `SkillRepository` with tenant-scoped draft creation, review submission,
  approval, rejection, active-version lookup, capability-filtered active recall
  and rollback.
- Draft/evaluating/review-pending/rejected versions are not returned by active
  recall.
- Activation atomically archives any previous active version and updates
  `skill_definitions.active_version_id`.
- Rollback atomically reactivates an archived/active target version.
- Manifest validation blocks write capabilities, tool creation and script-backed
  skills in V1.
- `create_app()` wires `app.state.skill_repository` when a database is configured.

## Acceptance evidence

Focused tests:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_skill_repository.py tests\test_capability_bundles.py -q
9 passed in 3.15s

D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_skill_repository.py tests\test_bootstrap_boundaries.py tests\test_health.py -q
12 passed in 10.84s
```

Migration rendering:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m alembic upgrade head --sql
Generated through revision 20260719_0006_skill_lifecycle
```

## Rollback note

Downgrade Alembic from `20260719_0006` to `20260719_0005` to drop only
`skill_versions` and `skill_definitions`. The rollback does not affect tasks,
approval plans, alerts, environments or managed LLM credentials.
