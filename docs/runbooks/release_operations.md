# Athena Release Operations Runbook

## One-command local demo

Use Docker with the daemon running:

```powershell
$env:ATHENA_SECRET_MASTER_KEY = "<stable-fernet-or-high-entropy-key>"
$env:ATHENA_ALERT_INTEGRATION_TOKENS = "<integration-token>:<tenant>"
docker compose up --build
```

The API is `athena-api`, the worker is `athena-worker`, PostgreSQL is the source
of truth, and Redis is the at-least-once delivery/cache backend.

## Backup

PostgreSQL:

```bash
pg_dump --format=custom --file=/backup/athena-$(date +%Y%m%d%H%M%S).dump "$ATHENA_DATABASE_URL"
```

Evidence content:

```bash
tar -C /app -czf /backup/athena-evidence-$(date +%Y%m%d%H%M%S).tgz data/evidence
```

## Restore

1. Stop API and Worker.
2. Restore PostgreSQL with `pg_restore --clean --if-exists`.
3. Restore `data/evidence`.
4. Run `alembic upgrade head`.
5. Start API, wait for `/readyz`, then start Worker.

## Upgrade

1. Deploy migration job first.
2. Roll API after migration succeeds.
3. Roll Worker after API is ready.
4. Verify `/readyz`, alert webhook 202 acceptance, and one readonly OpsTask.

## Rollback

Use expand-first migrations. Prefer image rollback without destructive database
downgrade. If a revision must be downgraded, only downgrade the last additive
revision after confirming no code path still reads its new tables.

## Secret rotation

1. Add a new `ATHENA_SECRET_MASTER_KEY` only after re-encrypting managed
   credentials through the SecretStore rotation path.
2. Rotate `ATHENA_ALERT_INTEGRATION_TOKENS` by adding new token, updating
   Alertmanager, then removing the old token.
3. Never log raw API keys, alert tokens or LLM credentials.
