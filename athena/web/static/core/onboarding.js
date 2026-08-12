import { ApiError } from "./api.js";

export const ONBOARDING_STEPS = Object.freeze([
  Object.freeze({ id: "kubernetes", label: "Connect Kubernetes" }),
  Object.freeze({ id: "llm", label: "Configure LLM" }),
  Object.freeze({ id: "prometheus", label: "Connect Prometheus (optional)" }),
  Object.freeze({ id: "inspection", label: "Run readonly inspection" }),
]);

const USABLE_RESOURCE_STATUSES = new Set(["ready", "empty"]);
const ENVIRONMENT_TYPES = new Set(["kubernetes", "prometheus"]);
const ENVIRONMENT_MODES = new Set(["live", "replay", "mock"]);
const MODEL_PROVIDERS = new Set([
  "anthropic",
  "dashscope",
  "deepseek",
  "gemini",
  "openai",
]);
const ACTIVE_TASK_STATUSES = new Set(["queued", "running", "waiting"]);
const PUBLIC_CODE = /^[A-Z0-9_]{1,80}$/;

function asString(value) {
  return typeof value === "string" ? value : "";
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function projectionStatus(resource) {
  return asString(resource?.status) || "idle";
}

function projectionItems(resource) {
  return asArray(resource?.items);
}

function publicCode(value, fallback) {
  const code = asString(value).toUpperCase();
  return PUBLIC_CODE.test(code) ? code : fallback;
}

function publicEnvironment(value) {
  return Object.freeze({
    id: asString(value?.id),
    name: asString(value?.name) || "Unnamed environment",
    type: asString(value?.type) || "unknown",
    provider: asString(value?.provider) || "unknown",
    mode: asString(value?.mode) || "unknown",
    status: asString(value?.status) || "unknown",
  });
}

function publicModel(value) {
  return Object.freeze({
    configId: asString(value?.configId),
    displayName: asString(value?.displayName) || "Unnamed model",
    provider: asString(value?.provider) || "unknown",
    model: asString(value?.model) || "unknown",
    hasApiKey: value?.hasApiKey === true,
    enabled: value?.enabled === true,
    isDefault: value?.isDefault === true,
    status: asString(value?.status) || "unknown",
  });
}

function publicTask(value) {
  return Object.freeze({
    id: asString(value?.id),
    status: asString(value?.status) || "unknown",
    phase: asString(value?.phase) || "unknown",
    environmentId: asString(value?.environmentId),
  });
}

function connectionStepState(resourceStatus, configured, available) {
  if (!isProjectionUsable(resourceStatus)) return resourceStatus;
  if (!configured) return "missing";
  return available ? "complete" : "needs_test";
}

function modelIsAvailable(model) {
  return model.enabled
    && model.status === "available"
    && (model.hasApiKey || model.provider === "ollama");
}

function inspectionFor(tasks, environmentIds) {
  const candidates = tasks.filter((task) => environmentIds.has(task.environmentId));
  const succeeded = candidates.find((task) => task.status === "succeeded");
  const active = candidates.find((task) => ACTIVE_TASK_STATUSES.has(task.status));
  return succeeded || active || null;
}

export function isProjectionUsable(status) {
  return USABLE_RESOURCE_STATUSES.has(status);
}

/**
 * Completion is a projection of tenant-owned facts, never a browser flag.
 * The optional Prometheus skip is deliberately caller-owned volatile state.
 */
export function deriveOnboardingFacts({
  connections,
  models,
  tasks,
  prometheusSkipped = false,
} = {}) {
  const connectionStatus = projectionStatus(connections);
  const modelStatus = projectionStatus(models);
  const taskStatus = projectionStatus(tasks);
  const environments = projectionItems(connections).map(publicEnvironment).filter((item) => item.id);
  const availableKubernetes = environments.filter(
    (item) => item.type === "kubernetes" && item.status === "available",
  );
  const kubernetes = environments.filter((item) => item.type === "kubernetes");
  const prometheus = environments.filter((item) => item.type === "prometheus");
  const availablePrometheus = prometheus.filter((item) => item.status === "available");
  const modelItems = projectionItems(models).map(publicModel).filter((item) => item.configId);
  const availableModels = modelItems.filter(modelIsAvailable);
  const taskItems = projectionItems(tasks).map(publicTask).filter((item) => item.id);
  const inspectionTask = inspectionFor(
    taskItems,
    new Set(availableKubernetes.map((item) => item.id)),
  );

  const kubernetesState = connectionStepState(
    connectionStatus,
    kubernetes.length > 0,
    availableKubernetes.length > 0,
  );
  const llmState = connectionStepState(
    modelStatus,
    modelItems.length > 0,
    availableModels.length > 0,
  );
  const prometheusState = connectionStepState(
    connectionStatus,
    prometheus.length > 0,
    availablePrometheus.length > 0,
  );
  const inspectionState = !isProjectionUsable(taskStatus)
    ? taskStatus
    : inspectionTask?.status === "succeeded"
      ? "complete"
      : inspectionTask && ACTIVE_TASK_STATUSES.has(inspectionTask.status)
        ? "running"
        : "ready";

  let nextStep = "complete";
  if (kubernetesState !== "complete") nextStep = "kubernetes";
  else if (llmState !== "complete") nextStep = "llm";
  else if (prometheusState !== "complete" && !prometheusSkipped) nextStep = "prometheus";
  else if (inspectionState !== "complete") nextStep = "inspection";

  return Object.freeze({
    nextStep,
    completed: nextStep === "complete",
    kubernetes: Object.freeze({
      resourceStatus: connectionStatus,
      state: kubernetesState,
      configured: kubernetes.length > 0,
      available: availableKubernetes.length > 0,
      primary: availableKubernetes[0] || kubernetes[0] || null,
    }),
    llm: Object.freeze({
      resourceStatus: modelStatus,
      state: llmState,
      configured: modelItems.length > 0,
      available: availableModels.length > 0,
      primary: availableModels[0] || modelItems[0] || null,
    }),
    prometheus: Object.freeze({
      resourceStatus: connectionStatus,
      state: prometheusState,
      skipped: prometheusSkipped === true,
      configured: prometheus.length > 0,
      available: availablePrometheus.length > 0,
      primary: availablePrometheus[0] || prometheus[0] || null,
    }),
    inspection: Object.freeze({
      resourceStatus: taskStatus,
      state: inspectionState,
      task: inspectionTask,
      environment: availableKubernetes[0] || null,
    }),
  });
}

export class OnboardingInputError extends Error {
  constructor(code = "ONBOARDING_INPUT_INVALID") {
    super(code);
    this.name = "OnboardingInputError";
    this.code = code;
  }
}

function requiredText(value, { maxLength = 120 } = {}) {
  const text = asString(value).trim();
  if (!text || text.length > maxLength) throw new OnboardingInputError();
  return text;
}

function optionalText(value, { maxLength = 120 } = {}) {
  const text = asString(value).trim();
  if (text.length > maxLength) throw new OnboardingInputError();
  return text || null;
}

function requiredId(value) {
  const identifier = requiredText(value, { maxLength: 160 });
  if (!/^[A-Za-z0-9._:-]+$/.test(identifier)) {
    throw new OnboardingInputError("ONBOARDING_RESPONSE_INVALID");
  }
  return identifier;
}

function actionResult(status, errorCode = null) {
  return Object.freeze({
    status,
    errorCode: errorCode ? publicCode(errorCode, "ONBOARDING_REQUEST_FAILED") : null,
  });
}

function failureResult(error) {
  if (error instanceof OnboardingInputError) {
    return actionResult("error", publicCode(error.code, "ONBOARDING_INPUT_INVALID"));
  }
  if (error instanceof ApiError && [401, 403].includes(error.status)) {
    return actionResult("forbidden", publicCode(error.code, "FORBIDDEN"));
  }
  if (error instanceof ApiError) {
    return actionResult("error", publicCode(error.code, "ONBOARDING_REQUEST_FAILED"));
  }
  return actionResult("error", "ONBOARDING_REQUEST_FAILED");
}

async function refreshQuietly(refresh) {
  if (typeof refresh !== "function") return;
  try {
    await refresh();
  } catch {
    // A completed server mutation remains valid even if the read projection lags.
  }
}

function defaultIdempotencyKey() {
  const uuid = globalThis.crypto?.randomUUID?.();
  return uuid
    ? `onboarding-${uuid}`
    : `onboarding-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * This action layer accepts a client at the composition root so it can be
 * verified without a browser. It only returns generic, display-safe outcomes.
 */
export function createOnboardingActions({
  client,
  refresh,
  idempotencyKeyFactory = defaultIdempotencyKey,
} = {}) {
  if (!client || typeof client.post !== "function") {
    throw new TypeError("An API client with post() is required");
  }

  async function run(operation) {
    try {
      return await operation();
    } catch (error) {
      return failureResult(error);
    }
  }

  async function testEnvironment(environmentId) {
    return run(async () => {
      const id = requiredId(environmentId);
      const result = await client.post(`/api/environments/${encodeURIComponent(id)}/test`);
      await refreshQuietly(refresh);
      return result?.status === "available"
        ? actionResult("succeeded")
        : actionResult("error", result?.reason_code || "ENVIRONMENT_CONNECTION_FAILED");
    });
  }

  async function saveAndTestEnvironment({
    environmentType,
    name,
    provider,
    mode,
    namespace,
  } = {}) {
    return run(async () => {
      const type = requiredText(environmentType, { maxLength: 40 }).toLowerCase();
      if (!ENVIRONMENT_TYPES.has(type)) throw new OnboardingInputError();
      const environmentMode = requiredText(mode, { maxLength: 20 }).toLowerCase();
      if (!ENVIRONMENT_MODES.has(environmentMode)) throw new OnboardingInputError();
      const payload = {
        name: requiredText(name),
        environment_type: type,
        provider: requiredText(provider, { maxLength: 80 }),
        mode: environmentMode,
        scope: { namespaces: [optionalText(namespace) || "default"] },
      };
      const created = await client.post("/api/environments", payload);
      return testEnvironment(requiredId(created?.id));
    });
  }

  async function testModel(configId) {
    return run(async () => {
      const id = requiredId(configId);
      const result = await client.post(`/api/llm/configs/${encodeURIComponent(id)}/test`);
      await refreshQuietly(refresh);
      return result?.success === true
        ? actionResult("succeeded")
        : actionResult("error", result?.reason_code || "LLM_CONNECTION_FAILED");
    });
  }

  async function saveAndTestModel({ provider, displayName, model, apiKey } = {}) {
    return run(async () => {
      const normalizedProvider = requiredText(provider, { maxLength: 40 }).toLowerCase();
      if (!MODEL_PROVIDERS.has(normalizedProvider)) throw new OnboardingInputError();
      let credential = requiredText(apiKey, { maxLength: 300 });
      let created;
      try {
        created = await client.post("/api/llm/configs", {
          provider: normalizedProvider,
          display_name: requiredText(displayName, { maxLength: 80 }),
          model: requiredText(model, { maxLength: 120 }),
          api_key: credential,
          enabled: true,
          is_default: true,
        });
      } finally {
        credential = "";
      }
      return testModel(requiredId(created?.config_id));
    });
  }

  async function runReadonlyInspection({ environmentId, namespace = "default" } = {}) {
    return run(async () => {
      const key = requiredText(idempotencyKeyFactory(), { maxLength: 200 });
      await client.post(
        "/api/ops/tasks",
        {
          objective: "Run an initial readonly Kubernetes health inspection.",
          environment_id: requiredId(environmentId),
          namespace: optionalText(namespace) || "default",
        },
        { headers: { "Idempotency-Key": key } },
      );
      await refreshQuietly(refresh);
      return actionResult("succeeded");
    });
  }

  return Object.freeze({
    saveAndTestEnvironment,
    testEnvironment,
    saveAndTestModel,
    testModel,
    runReadonlyInspection,
  });
}

/** Volatile UX choices only; never serialized to browser storage. */
export function createOnboardingUiState() {
  let state = Object.freeze({
    prometheusSkipped: false,
    action: Object.freeze({ step: null, status: "idle", errorCode: null }),
  });

  function replace(next) {
    state = Object.freeze({
      prometheusSkipped: next.prometheusSkipped === true,
      action: Object.freeze({
        step: typeof next.action?.step === "string" ? next.action.step : null,
        status: typeof next.action?.status === "string" ? next.action.status : "idle",
        errorCode: next.action?.errorCode
          ? publicCode(next.action.errorCode, "ONBOARDING_REQUEST_FAILED")
          : null,
      }),
    });
    return state;
  }

  return Object.freeze({
    getState: () => state,
    skipPrometheus: () => replace({ ...state, prometheusSkipped: true }),
    beginAction: (step) => replace({
      ...state,
      action: { step, status: "loading", errorCode: null },
    }),
    finishAction: (step, result) => replace({
      ...state,
      action: {
        step,
        status: result?.status || "error",
        errorCode: result?.errorCode || null,
      },
    }),
  });
}
