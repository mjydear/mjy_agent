"""Static contracts for the native ES module migration foundation."""

from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "athena" / "web" / "static"


def read(relative_path: str) -> str:
    return (STATIC / relative_path).read_text(encoding="utf-8")


def test_module_foundation_has_the_planned_native_es_module_layout() -> None:
    required = {
        "core/api.js",
        "core/approvals.js",
        "core/projections.js",
        "core/router.js",
        "core/store.js",
        "core/onboarding.js",
        "pages/overview.js",
        "pages/operations.js",
        "pages/alerts.js",
        "pages/approvals.js",
        "pages/connections.js",
        "pages/audit.js",
        "pages/model-settings.js",
        "pages/onboarding.js",
        "components/status-badge.js",
        "components/task-timeline.js",
        "components/evidence-panel.js",
        "components/dialog.js",
        "components/empty-state.js",
        "components/projection-state.js",
        "styles/tokens.css",
        "styles/layout.css",
        "styles/components.css",
        "styles/pages.css",
        "main.js",
    }
    assert all((STATIC / item).is_file() for item in required)

    entrypoint = read("main.js")
    assert 'from "./core/api.js"' in entrypoint
    assert 'from "./core/approvals.js"' in entrypoint
    assert 'from "./core/router.js"' in entrypoint
    assert 'from "./core/store.js"' in entrypoint
    assert 'from "./core/onboarding.js"' in entrypoint
    assert "bootstrapFrontend" in entrypoint


def test_module_state_is_memory_only_and_separates_domain_projections() -> None:
    store = read("core/store.js")
    assert "appState" in store
    assert "taskStore" in store
    assert "connectionStore" in store
    assert "operationPlanStore" in store
    assert "approvalStore" in store
    assert "modelStore" in store
    assert "sessionStore" in store
    assert "localStorage" not in store
    assert "sessionStorage" not in store

    api = read("core/api.js")
    assert "ApiError" in api
    assert "Authorization" not in api


def test_module_projection_loader_uses_only_safe_metadata_and_explicit_states() -> None:
    projections = read("core/projections.js")
    main = read("main.js")
    components = read("components/projection-state.js")
    operations = read("pages/operations.js")
    connections = read("pages/connections.js")
    models = read("pages/model-settings.js")
    approvals = read("pages/approvals.js")

    for endpoint in (
        '"/readyz"',
        '"/api/ops/tasks?limit=20"',
        '"/api/environments"',
        '"/api/llm/configs"',
        '"/api/operation-plans?limit=20"',
        '"/api/approvals?limit=20"',
    ):
        assert endpoint in projections
    for state in ("loading", "empty", "forbidden", "degraded", "error"):
        assert f"{state}:" in components
    assert "loadDashboardProjections" in main
    assert "refreshModuleProjections" in main
    assert "if (modulePreviewEnabled())" in main
    assert "Preserve the P1 console request pattern" in main
    assert "credential_ref" not in connections
    assert "masked_api_key" not in models
    assert "base_url" not in models
    assert "task?.objective" not in operations
    assert "raw operator prompt" not in projections
    assert "parameters" not in approvals
    assert "canonical" not in approvals


def test_index_uses_local_css_and_adds_module_entrypoint_without_removing_p1() -> None:
    index = read("index.html")
    assert "https://cdn.tailwindcss.com" not in index
    for stylesheet in (
        "/static/styles/tokens.css",
        "/static/styles/layout.css",
        "/static/styles/components.css",
        "/static/styles/pages.css",
    ):
        assert stylesheet in index
    assert 'id="legacy-console"' in index
    assert "/static/ops-task-workbench.js" in index
    assert 'type="module" src="/static/main.js' in index


def test_hash_router_exposes_all_console_paths() -> None:
    router = read("core/router.js")
    for route in (
        'pattern: "/overview"',
        'pattern: "/operations"',
        'pattern: "/operations/:taskId"',
        'pattern: "/alerts"',
        'pattern: "/connections"',
        'pattern: "/approvals"',
        'pattern: "/audit"',
        'pattern: "/settings/models"',
        'pattern: "/onboarding"',
    ):
        assert route in router


def test_onboarding_uses_derived_safe_facts_and_existing_api_actions() -> None:
    onboarding = read("core/onboarding.js")
    page = read("pages/onboarding.js")
    main = read("main.js")

    for endpoint in (
        '"/api/environments"',
        '"/api/llm/configs"',
        '"/api/ops/tasks"',
        "/test`",
    ):
        assert endpoint in onboarding
    assert "deriveOnboardingFacts" in onboarding
    assert "createOnboardingUiState" in onboarding
    assert "localStorage" not in onboarding
    assert "sessionStorage" not in onboarding
    assert "localStorage" not in page
    assert "sessionStorage" not in page
    assert "credential_ref" not in onboarding
    assert "masked_api_key" not in onboarding
    assert "error.message" not in onboarding
    assert "onboarding: renderOnboarding" in main
    assert "renderOnboarding" in main


def test_approval_page_uses_hash_approval_and_idempotency_boundaries() -> None:
    approvals = read("core/approvals.js")
    page = read("pages/approvals.js")
    main = read("main.js")

    for token in (
        "/api/approvals/",
        "/approve",
        "/reject",
        "/execute",
        "Idempotency-Key",
        "plan_hash",
    ):
        assert token in approvals
    assert "error.message" not in approvals
    assert "APPROVAL_REQUEST_FAILED" in approvals
    assert "renderApprovals" in page
    assert "Approval queue" in page
    assert "Writable actions require immutable plans" in page
    assert "approvals: renderApprovals" in main
