import { createEmptyState } from "../components/empty-state.js";
import { createProjectionState } from "../components/projection-state.js";
import { createStatusBadge } from "../components/status-badge.js";
import { appendSection, replacePage } from "./page.js";

function createActionButton(label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "module-action-button";
  button.textContent = label;
  if (typeof handler === "function") button.addEventListener("click", handler);
  return button;
}

function resourceContent(resource, { emptyTitle, emptyMessage, renderItems }) {
  const status = resource?.status || "idle";
  if (status !== "ready") {
    return createProjectionState(status, { emptyTitle, emptyMessage });
  }
  return renderItems(resource.items || []);
}

function createTaskList(tasks) {
  if (!tasks.length) {
    return createEmptyState({
      title: "No recent tasks",
      message: "Create a readonly task after connecting an environment.",
    });
  }
  const list = document.createElement("ul");
  list.className = "module-projection-list";
  tasks.forEach((task) => {
    const row = document.createElement("li");
    const details = document.createElement("div");
    const title = document.createElement("strong");
    const meta = document.createElement("small");
    title.textContent = `Task ${task.id}`;
    meta.textContent = `${task.phase} | ${task.environmentMode}`;
    details.append(title, meta);
    row.append(details, createStatusBadge(task.status));
    list.append(row);
  });
  return list;
}

function createEnvironmentSummary(environments) {
  const unavailable = environments.filter((item) => item.status !== "available").length;
  const summary = document.createElement("div");
  summary.className = "module-health-summary";
  summary.append(
    createStatusBadge(unavailable ? "degraded" : "healthy"),
    document.createTextNode(
      unavailable
        ? `${unavailable} environment connection${unavailable === 1 ? "" : "s"} need attention`
        : "All configured environment connections are available",
    ),
  );
  return summary;
}

function createModelSummary(models) {
  const available = models.filter((item) => item.enabled && item.status === "available");
  const summary = document.createElement("div");
  summary.className = "module-health-summary";
  summary.append(
    createStatusBadge(available.length ? "healthy" : "degraded"),
    document.createTextNode(
      available.length
        ? `${available.length} model configuration${available.length === 1 ? "" : "s"} available`
        : "No available model configuration",
    ),
  );
  return summary;
}

export function renderOverview(container, {
  health,
  tasks,
  connections,
  models,
  onboardingActions,
} = {}) {
  const page = replacePage(container, {
    eyebrow: "CloudOps",
    title: "Overview",
    description: "Tenant-scoped service, task, environment, and model metadata is loaded on demand.",
  });
  const status = document.createElement("div");
  status.className = "module-health-summary";
  status.append(createStatusBadge(health?.status || "loading"));
  if (health?.status === "degraded") {
    const message = document.createElement("span");
    message.className = "module-degraded-notice";
    message.textContent = "One or more dependencies are degraded. Results may be partial.";
    status.append(message);
  }
  appendSection(page, "Global health", status);
  if (Array.isArray(health?.components) && health.components.length) {
    const components = document.createElement("ul");
    components.className = "module-health-components";
    health.components.forEach((component) => {
      const row = document.createElement("li");
      const name = document.createElement("strong");
      const detail = document.createElement("small");
      name.textContent = component.component;
      detail.textContent = `${component.configuredBackend} -> ${component.activeBackend}`;
      row.append(name, detail, createStatusBadge(component.status));
      components.append(row);
    });
    appendSection(page, "Dependencies", components);
  }
  appendSection(
    page,
    "Recent work",
    resourceContent(tasks, {
      emptyTitle: "No recent tasks",
      emptyMessage: "Create a readonly task after connecting an environment.",
      renderItems: createTaskList,
    }),
  );
  appendSection(
    page,
    "Environment health",
    resourceContent(connections, {
      emptyTitle: "No environments",
      emptyMessage: "Connect Kubernetes or Prometheus before running a readonly task.",
      renderItems: createEnvironmentSummary,
    }),
  );
  if (connections?.status === "empty") {
    const start = document.createElement("div");
    start.className = "module-onboarding-controls";
    start.append(createActionButton("Connect environment", () => {
      onboardingActions?.startOnboarding?.();
    }));
    appendSection(page, "Get started", start);
  }
  appendSection(
    page,
    "Model availability",
    resourceContent(models, {
      emptyTitle: "No model configuration",
      emptyMessage: "Model-backed diagnosis remains unavailable until a model is configured.",
      renderItems: createModelSummary,
    }),
  );
}
