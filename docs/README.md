# Athena Documentation

This directory contains design, operation, verification, and interview material.
The current implementation is the source of truth; documents describe boundaries
and acceptance evidence, not capabilities that are absent from the code.

## Start Here

- [Architecture](features/enterprise_cloudops_agent_architecture.md)
- [Implementation Plan](features/enterprise_cloudops_agent_implementation_plan.md)
- [Development Guide](guides/development.md)
- [Deployment Guide](guides/deployment.md)
- [API Reference](reference/api.md)

## By Purpose

- `features/`: bounded product capabilities and architecture decisions.
- `benchmarks/`: reproducible performance and evaluation reports.
- `evidence/`: implementation acceptance evidence and release history.
- `runbooks/`: operational procedures for release and recovery.
- `interview/`: concise project explanation and interview material.
- `demos/`: commands for the supported local demonstrations.
- `architecture/`: small diagrams used by the design documents.

## Current CloudOps Scope

The supported product storyline is Kubernetes Pod alert diagnosis. The first
workflow set covers CrashLoop, Pending, ImagePull, and Resource Pressure alerts.
The Agent is read-only by default, persists verified outcomes, and requires
operator approval for any environment-changing action.
