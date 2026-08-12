# Phase 5 Release Artifact Evidence

Date: 2026-07-19

## Scope

This evidence covers P5-04 Compose, Helm, backup/restore and release artifacts.

## Implementation facts

- `docker-compose.yml` declares PostgreSQL, Redis, migration job, `athena-api`
  and `athena-worker`.
- API and Worker use the same image/build context with different commands.
- Compose service names are unified; the old ambiguous `athena` service name is
  replaced by `athena-api`.
- Helm chart added under `deploy/helm/athena` with:
  - API Deployment
  - Worker Deployment
  - Migration Job
  - Secret example
  - ServiceAccount
  - Service
  - Ingress
  - PodDisruptionBudget
  - HorizontalPodAutoscaler
  - NetworkPolicy
- Release runbook added at `docs/runbooks/release_operations.md`, covering
  one-command local demo, backup, restore, upgrade, rollback and secret rotation.

## Acceptance evidence

Focused tests:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_release_artifacts.py tests\test_bootstrap_boundaries.py -q
5 passed in 0.71s
```

## Environment limitation

Docker daemon is not currently running locally, so this evidence validates the
release artifacts statically but does not claim a live Compose/Helm deployment.

## Rollback note

Revert `docker-compose.yml` service-name changes and remove `deploy/helm/athena`
plus `docs/runbooks/release_operations.md`. No database rollback is required.
