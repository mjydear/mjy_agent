# Enterprise Sandbox And Permissions

## Design Goal

Athena extends the sandbox with fine-grained permissions and audit logs. Tool execution can be controlled by risk level, directory scope, network host, and human confirmation.

## Key Decisions

- `PermissionManager` blocks tools without explicit policy.
- High-risk tools can require human confirmation.
- `AuditLogger` records full-chain tool events in memory and optional JSONL files.

## Interview Talking Points

- Production agents need permission boundaries, not just prompt instructions.
- Read/write/high-risk separation mirrors real SRE operation risk models.
- Audit logs enable replay, accountability, and incident review.