# Phase 4 Public Bootstrap and Compatibility Facade Evidence

Date: 2026-07-19

## Scope

This evidence covers P4-04 Public bootstrap and compatibility facade split.

## Implementation facts

- Added `athena/bootstrap/agent_factory.py` as the public Agent composition root.
- Added `athena/bootstrap/__init__.py` to expose `build_agent`.
- `athena/api/server.py` now imports `build_agent` from `athena.bootstrap`, not
  from the CLI entrypoint.
- `AthenaWebService` dynamic per-request LLM config rebuild path now imports
  `build_agent` from `athena.bootstrap`.
- `athena/cli/main.py` preserves the existing `build_agent` public name, but that
  name now points at the shared bootstrap implementation so CLI and API use the
  same composition root.

## Acceptance evidence

Focused tests:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_bootstrap_boundaries.py tests\test_llm_config_secrets.py tests\test_web_console.py tests\test_health.py -q
31 passed in 23.35s
```

Boundary check:

```text
rg -n "athena\.cli\.main import build_agent|from athena\.cli\.main import build_agent" athena tests -S
```

The only remaining occurrences are in `tests/test_bootstrap_boundaries.py`,
which intentionally asserts that the API layer does not import the CLI
composition root.

Code health:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m compileall -q athena tests
git diff --check
```

`git diff --check` reported only existing LF/CRLF warnings and no whitespace
errors.

## Rollback note

The split is source-compatible for existing CLI callers because
`athena.cli.main.build_agent` remains importable. To roll back, point
`athena/api/server.py` and `AthenaWebService` back to the CLI symbol and remove
`athena/bootstrap`; no schema/data rollback is required.
