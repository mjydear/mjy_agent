import { ApiError } from "./api.js";

const RESOURCE_STATUSES = new Set([
  "idle",
  "loading",
  "ready",
  "empty",
  "forbidden",
  "degraded",
  "error",
]);

function asString(value) {
  return typeof value === "string" ? value : "";
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function asRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function copyStringList(value) {
  return asArray(value).filter((item) => typeof item === "string");
}

function publicErrorCode(error) {
  if (error instanceof ApiError && typeof error.code === "string") return error.code;
  return "PROJECTION_UNAVAILABLE";
}

export function resourceStatus({ items, error } = {}) {
  if (error instanceof ApiError && [401, 403].includes(error.status)) {
    return "forbidden";
  }
  if (error) return "error";
  return asArray(items).length ? "ready" : "empty";
}

export function initialResourceState() {
  return Object.freeze({ status: "idle", errorCode: null });
}

export function normalizeTask(value) {
  const task = asRecord(value);
  return Object.freeze({
    id: asString(task.id),
    status: asString(task.status) || "unknown",
    phase: asString(task.phase) || "unknown",
    environmentId: asString(task.environment_id),
    environmentMode: asString(task.environment_mode) || "unknown",
    degraded: task.degraded === true,
    degradationReasonCode: asString(task.degradation_reason_code) || null,
    eventCount: Number.isFinite(task.event_count) ? task.event_count : null,
    evidenceCount: Number.isFinite(task.evidence_count) ? task.evidence_count : null,
  });
}

export function normalizeEnvironment(value) {
  const environment = asRecord(value);
  return Object.freeze({
    id: asString(environment.id),
    name: asString(environment.name) || "Unnamed environment",
    type: asString(environment.type) || "unknown",
    provider: asString(environment.provider) || "unknown",
    mode: asString(environment.mode) || "unknown",
    capabilities: copyStringList(environment.capabilities),
    status: asString(environment.status) || "unknown",
    lastCheckedAt: asString(environment.last_checked_at) || null,
  });
}

export function normalizeModel(value) {
  const model = asRecord(value);
  return Object.freeze({
    configId: asString(model.config_id),
    displayName: asString(model.display_name) || "Unnamed model",
    provider: asString(model.provider) || "unknown",
    model: asString(model.model) || "unknown",
    hasApiKey: model.has_api_key === true,
    enabled: model.enabled === true,
    isDefault: model.is_default === true,
    status: asString(model.status) || "unknown",
  });
}

export function normalizeOperationPlan(value) {
  const plan = asRecord(value);
  return Object.freeze({
    id: asString(plan.id),
    taskId: asString(plan.task_id) || null,
    environmentId: asString(plan.environment_id),
    actionType: asString(plan.action_type) || "unknown",
    resourceKind: asString(plan.resource_kind) || "unknown",
    resourceName: asString(plan.resource_name) || "unknown",
    namespace: asString(plan.namespace) || "unknown",
    riskLevel: asString(plan.risk_level) || "unknown",
    requiredScope: asString(plan.required_scope) || "unknown",
    planHash: asString(plan.plan_hash),
    status: asString(plan.status) || "unknown",
    expiresAt: asString(plan.expires_at) || null,
  });
}

export function normalizeApproval(value) {
  const approval = asRecord(value);
  return Object.freeze({
    id: asString(approval.id),
    planId: asString(approval.plan_id),
    planHash: asString(approval.plan_hash),
    status: asString(approval.status) || "unknown",
    requestedAt: asString(approval.requested_at) || null,
    decidedAt: asString(approval.decided_at) || null,
    scopes: copyStringList(approval.scopes),
    expiresAt: asString(approval.expires_at) || null,
  });
}

export function normalizeHealth(value) {
  const health = asRecord(value);
  const components = asArray(health.components).map((item) => {
    const component = asRecord(item);
    return Object.freeze({
      component: asString(component.component) || "unknown",
      configuredBackend: asString(component.configured_backend) || "unknown",
      activeBackend: asString(component.active_backend) || "unknown",
      status: asString(component.status) || "unknown",
      reasonCode: asString(component.reason_code) || null,
    });
  });
  const degraded = health.ready !== true || components.some(
    (component) => ["degraded", "unavailable"].includes(component.status),
  );
  return Object.freeze({
    status: degraded ? "degraded" : "healthy",
    components,
  });
}

function listPayload(value) {
  const payload = asRecord(value);
  return asArray(payload.items);
}

function resultFor(items, error) {
  const status = resourceStatus({ items, error });
  return Object.freeze({
    items,
    status,
    errorCode: error ? publicErrorCode(error) : null,
  });
}

async function settle(request, normalizeItems) {
  try {
    const response = await request();
    return resultFor(normalizeItems(response), null);
  } catch (error) {
    return resultFor([], error);
  }
}

/**
 * Fetch each read-only dashboard projection independently. A failed optional
 * projection must not hide the usable projections that were returned by the
 * same request cycle.
 */
export async function loadDashboardProjections(client) {
  const [healthResult, tasks, environments, models, plans, approvals] = await Promise.all([
    settle(
      () => client.get("/readyz"),
      (response) => [normalizeHealth(response)],
    ),
    settle(
      () => client.get("/api/ops/tasks?limit=20"),
      (response) => listPayload(response).map(normalizeTask).filter((task) => task.id),
    ),
    settle(
      () => client.get("/api/environments"),
      (response) => listPayload(response).map(normalizeEnvironment).filter((item) => item.id),
    ),
    settle(
      () => client.get("/api/llm/configs"),
      (response) => asArray(response).map(normalizeModel).filter((item) => item.configId),
    ),
    settle(
      () => client.get("/api/operation-plans?limit=20"),
      (response) => listPayload(response).map(normalizeOperationPlan).filter((item) => item.id),
    ),
    settle(
      () => client.get("/api/approvals?limit=20"),
      (response) => listPayload(response).map(normalizeApproval).filter((item) => item.id),
    ),
  ]);

  const health = healthResult.items[0] || Object.freeze({
    status: "degraded",
    components: [],
  });
  return Object.freeze({
    health: Object.freeze({
      ...health,
      status: healthResult.error ? "degraded" : health.status,
      errorCode: healthResult.errorCode,
    }),
    tasks,
    environments,
    models,
    plans,
    approvals,
  });
}

export async function loadTaskProjection(client, taskId) {
  try {
    const task = normalizeTask(
      await client.get(`/api/ops/tasks/${encodeURIComponent(taskId)}`),
    );
    if (!task.id) throw new TypeError("Task projection did not include an id");
    return Object.freeze({ task, status: "ready", errorCode: null });
  } catch (error) {
    const status = error instanceof ApiError && [401, 403].includes(error.status)
      ? "forbidden"
      : "error";
    return Object.freeze({ task: null, status, errorCode: publicErrorCode(error) });
  }
}

export function isResourceStatus(value) {
  return RESOURCE_STATUSES.has(value);
}
