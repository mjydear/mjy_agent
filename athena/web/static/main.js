import { api, ApiError } from "./core/api.js";
import { createApprovalActions } from "./core/approvals.js";
import { createHashRouter } from "./core/router.js";
import {
  appState,
  approvalStore,
  connectionStore,
  modelStore,
  operationPlanStore,
  sessionStore,
  taskStore,
} from "./core/store.js";
import {
  loadDashboardProjections,
  loadTaskProjection,
} from "./core/projections.js";
import {
  createOnboardingActions,
  createOnboardingUiState,
  deriveOnboardingFacts,
} from "./core/onboarding.js";
import { renderAlerts } from "./pages/alerts.js";
import { renderApprovals } from "./pages/approvals.js";
import { renderAudit } from "./pages/audit.js";
import { renderConnections } from "./pages/connections.js";
import { renderModelSettings } from "./pages/model-settings.js";
import { renderOnboarding } from "./pages/onboarding.js";
import { renderOperations } from "./pages/operations.js";
import { renderOverview } from "./pages/overview.js";
import { mountRuntimeConsole } from "./runtime-console.js";

const PAGE_RENDERERS = Object.freeze({
  overview: renderOverview,
  operations: renderOperations,
  "operation-detail": renderOperations,
  alerts: renderAlerts,
  approvals: renderApprovals,
  connections: renderConnections,
  audit: renderAudit,
  "model-settings": renderModelSettings,
  onboarding: renderOnboarding,
});

let activeRoute = null;
let activeRouter = null;
let dashboardRequest = 0;
let selectedTaskRequest = 0;
let dashboardLoaded = false;
const onboardingUiState = createOnboardingUiState();
const onboardingApiActions = createOnboardingActions({
  client: api,
  refresh: () => refreshModuleProjections(),
});
const approvalApiActions = createApprovalActions({
  client: api,
  refresh: () => refreshModuleProjections(),
});

function modulePreviewEnabled() {
  return typeof globalThis.window !== "undefined"
    && new URLSearchParams(globalThis.window.location.search).get("frontend") === "modules";
}

function runtimeConsoleEnabled() {
  return typeof globalThis.window !== "undefined"
    && new URLSearchParams(globalThis.window.location.search).get("frontend") === "runtime";
}

function pageContext(route) {
  const taskId = route.params.taskId;
  const tasks = taskStore.getState();
  const selected = tasks.selection || {};
  const selectedTask = selected.item?.id === taskId ? selected.item : tasks.byId[taskId];
  const onboardingUi = onboardingUiState.getState();
  return {
    health: appState.getState().health,
    task: taskId ? selectedTask : null,
    tasks,
    taskResource: {
      selected: Boolean(taskId),
      status: taskId ? selected.status || "idle" : tasks.status,
      errorCode: taskId ? selected.errorCode || null : tasks.errorCode,
    },
    connections: connectionStore.getState(),
    models: modelStore.getState(),
    plans: operationPlanStore.getState(),
    approvals: approvalStore.getState(),
    approvalActions: approvalApiActions,
    onboarding: deriveOnboardingFacts({
      connections: connectionStore.getState(),
      models: modelStore.getState(),
      tasks,
      prometheusSkipped: onboardingUi.prometheusSkipped,
    }),
    onboardingAction: onboardingUi.action,
    onboardingActions: onboardingPageActions(),
  };
}

function runOnboardingAction(step, operation) {
  onboardingUiState.beginAction(step);
  renderCurrentPreview();
  return Promise.resolve(operation())
    .then((result) => {
      onboardingUiState.finishAction(step, result);
      renderCurrentPreview();
      return result;
    })
    .catch(() => {
      const result = { status: "error", errorCode: "ONBOARDING_REQUEST_FAILED" };
      onboardingUiState.finishAction(step, result);
      renderCurrentPreview();
      return result;
    });
}

function onboardingPageActions() {
  return Object.freeze({
    saveAndTestEnvironment: (input) => runOnboardingAction(
      input?.environmentType === "prometheus" ? "prometheus" : "kubernetes",
      () => onboardingApiActions.saveAndTestEnvironment(input),
    ),
    testEnvironment: (input) => {
      const environmentId = typeof input === "string" ? input : input?.environmentId;
      const step = typeof input === "object" && input?.step === "prometheus"
        ? "prometheus"
        : "kubernetes";
      return runOnboardingAction(
        step,
        () => onboardingApiActions.testEnvironment(environmentId),
      );
    },
    saveAndTestModel: (input) => runOnboardingAction(
      "llm",
      () => onboardingApiActions.saveAndTestModel(input),
    ),
    testModel: (configId) => runOnboardingAction(
      "llm",
      () => onboardingApiActions.testModel(configId),
    ),
    runReadonlyInspection: (input) => runOnboardingAction(
      "inspection",
      () => onboardingApiActions.runReadonlyInspection(input),
    ),
    skipPrometheus: () => {
      onboardingUiState.skipPrometheus();
      renderCurrentPreview();
    },
    startOnboarding: () => activeRouter?.navigate("/onboarding"),
    viewInspection: (taskId) => activeRouter?.navigate(`/operations/${encodeURIComponent(taskId)}`),
    enterConsole: () => activeRouter?.navigate("/overview"),
  });
}

function renderPreviewRoute(route) {
  const host = document.getElementById("module-app");
  const legacyConsole = document.getElementById("legacy-console");
  if (!host || !legacyConsole) return;

  // The preview is opt-in while P2-05 migrates real page data and controls.
  if (!modulePreviewEnabled()) {
    host.hidden = true;
    legacyConsole.hidden = false;
    return;
  }

  host.hidden = false;
  legacyConsole.hidden = true;
  const renderer = PAGE_RENDERERS[route.name] || renderOverview;
  renderer(host, pageContext(route));
}

function renderCurrentPreview() {
  if (activeRoute) renderPreviewRoute(activeRoute);
}

function setDashboardLoading() {
  appState.patch({ health: { status: "loading", components: [] } });
  taskStore.setLoading();
  connectionStore.setLoading();
  modelStore.setLoading();
  operationPlanStore.setLoading();
  approvalStore.setLoading();
  renderCurrentPreview();
}

function applyProjection(store, projection) {
  store.replace(projection.items, {
    status: projection.status,
    errorCode: projection.errorCode,
  });
}

export async function refreshModuleProjections(client = api) {
  if (!modulePreviewEnabled()) return null;
  const request = ++dashboardRequest;
  setDashboardLoading();
  const projections = await loadDashboardProjections(client);
  if (request !== dashboardRequest) return null;

  appState.patch({ health: projections.health });
  applyProjection(taskStore, projections.tasks);
  applyProjection(connectionStore, projections.environments);
  applyProjection(modelStore, projections.models);
  applyProjection(operationPlanStore, projections.plans);
  applyProjection(approvalStore, projections.approvals);
  dashboardLoaded = true;
  renderCurrentPreview();

  if (activeRoute?.params.taskId) {
    await refreshSelectedTask(activeRoute.params.taskId, client);
  }
  return projections;
}

export async function refreshSelectedTask(taskId, client = api) {
  if (!modulePreviewEnabled() || !taskId) return null;
  const request = ++selectedTaskRequest;
  const knownTask = taskStore.getState().byId[taskId] || null;
  taskStore.setSelection(knownTask, { status: "loading" });
  renderCurrentPreview();
  const projection = await loadTaskProjection(client, taskId);
  if (request !== selectedTaskRequest || activeRoute?.params.taskId !== taskId) {
    return null;
  }
  if (projection.task) taskStore.upsert(projection.task);
  taskStore.setSelection(projection.task, {
    status: projection.status,
    errorCode: projection.errorCode,
  });
  renderCurrentPreview();
  return projection;
}

export async function refreshGlobalHealth(client = api) {
  try {
    const health = await client.get("/readyz");
    appState.patch({
      health: {
        status: health?.ready ? "healthy" : "degraded",
        components: Array.isArray(health?.components) ? health.components : [],
      },
    });
  } catch (error) {
    appState.patch({ health: { status: "degraded", components: [] } });
    if (!(error instanceof ApiError)) throw error;
  }
}

export function bootstrapFrontend() {
  if (runtimeConsoleEnabled()) {
    const legacyConsole = document.getElementById("legacy-console");
    const moduleApp = document.getElementById("module-app");
    const runtimeRoot = document.getElementById("runtime-console-root");
    if (!runtimeRoot) return null;
    if (legacyConsole) legacyConsole.hidden = true;
    if (moduleApp) moduleApp.hidden = true;
    runtimeRoot.hidden = false;
    const runtimeConsole = mountRuntimeConsole(runtimeRoot, { api });
    const frontend = Object.freeze({ api, runtimeConsole });
    globalThis.window.AthenaFrontend = frontend;
    return frontend;
  }
  const router = createHashRouter();
  activeRouter = router;
  router.subscribe((route) => {
    activeRoute = route;
    appState.patch({ route: { name: route.name, path: route.path, params: route.params } });
    document.documentElement.dataset.athenaRoute = route.name;
    renderPreviewRoute(route);
    if (modulePreviewEnabled() && dashboardLoaded && route.params.taskId) {
      refreshSelectedTask(route.params.taskId).catch(() => undefined);
    }
  });
  router.start();
  if (modulePreviewEnabled()) {
    refreshModuleProjections().catch(() => undefined);
  } else {
    // Preserve the P1 console request pattern unless the module preview is opted in.
    refreshGlobalHealth().catch(() => undefined);
  }

  const frontend = Object.freeze({
    api,
    router,
    refreshModuleProjections,
    stores: Object.freeze({
      appState,
      taskStore,
      connectionStore,
      modelStore,
      operationPlanStore,
      approvalStore,
      sessionStore,
    }),
  });
  globalThis.window.AthenaFrontend = frontend;
  return frontend;
}

bootstrapFrontend();
