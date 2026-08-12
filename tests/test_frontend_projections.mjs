import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../athena/web/static/core/api.js";
import { createApprovalActions } from "../athena/web/static/core/approvals.js";
import {
  loadDashboardProjections,
  loadTaskProjection,
  normalizeApproval,
  normalizeEnvironment,
  normalizeModel,
  normalizeOperationPlan,
  normalizeTask,
  resourceStatus,
} from "../athena/web/static/core/projections.js";
import {
  createOnboardingActions,
  createOnboardingUiState,
  deriveOnboardingFacts,
} from "../athena/web/static/core/onboarding.js";

test("metadata normalizers retain only browser-safe projection fields", () => {
  const task = normalizeTask({
    id: "task-1",
    objective: "raw operator prompt with secret=do-not-render",
    status: "running",
    phase: "collect",
    environment_id: "env-1",
    environment_mode: "mock",
    scope: { namespace: "payments" },
    budget: { tool_calls: 8 },
  });
  const environment = normalizeEnvironment({
    id: "env-1",
    name: "payments",
    type: "kubernetes",
    provider: "kind",
    mode: "mock",
    credential_ref: "secret://tenant/cluster",
    capabilities: ["k8s.workload.read"],
    status: "available",
  });
  const model = normalizeModel({
    config_id: "model-1",
    display_name: "Primary",
    provider: "openai",
    model: "gpt-test",
    has_api_key: true,
    masked_api_key: "****cret",
    base_url: "https://token@example.test",
    enabled: true,
    is_default: true,
    status: "available",
  });
  const plan = normalizeOperationPlan({
    id: "plan-1",
    action_type: "scale_deployment",
    resource_kind: "Deployment",
    resource_name: "checkout",
    namespace: "payments",
    risk_level: "S3",
    required_scope: "cloud:execute",
    plan_hash: "a".repeat(64),
    status: "approval_pending",
    parameters: { replicas: 10 },
    canonical: { hidden: "do-not-render" },
    dry_run: { raw: "kubectl" },
  });
  const approval = normalizeApproval({
    id: "approval-1",
    plan_id: "plan-1",
    plan_hash: "a".repeat(64),
    status: "pending",
    scopes: ["cloud:execute"],
    decision_note: "secret reason",
  });

  assert.deepEqual(task, {
    id: "task-1",
    status: "running",
    phase: "collect",
    environmentId: "env-1",
    environmentMode: "mock",
    degraded: false,
    degradationReasonCode: null,
    eventCount: null,
    evidenceCount: null,
  });
  assert.deepEqual(environment, {
    id: "env-1",
    name: "payments",
    type: "kubernetes",
    provider: "kind",
    mode: "mock",
    capabilities: ["k8s.workload.read"],
    status: "available",
    lastCheckedAt: null,
  });
  assert.deepEqual(model, {
    configId: "model-1",
    displayName: "Primary",
    provider: "openai",
    model: "gpt-test",
    hasApiKey: true,
    enabled: true,
    isDefault: true,
    status: "available",
  });
  assert.equal(plan.id, "plan-1");
  assert.equal(plan.actionType, "scale_deployment");
  assert.equal(plan.planHash, "a".repeat(64));
  assert.equal(approval.id, "approval-1");
  assert.deepEqual(approval.scopes, ["cloud:execute"]);
  const browserPayload = JSON.stringify({ task, environment, model, plan, approval });
  assert.equal(browserPayload.includes("raw operator prompt"), false);
  assert.equal(browserPayload.includes("credential_ref"), false);
  assert.equal(browserPayload.includes("secret://"), false);
  assert.equal(browserPayload.includes("****cret"), false);
  assert.equal(browserPayload.includes("token@example"), false);
  assert.equal(browserPayload.includes("replicas"), false);
  assert.equal(browserPayload.includes("do-not-render"), false);
  assert.equal(browserPayload.includes("secret reason"), false);
});

test("dashboard projections load independently and classify forbidden responses", async () => {
  const calls = [];
  const client = {
    async get(path) {
      calls.push(path);
      if (path === "/readyz") {
        return {
          ready: true,
          components: [
            {
              component: "cache",
              configured_backend: "memory",
              active_backend: "memory",
              status: "healthy",
            },
          ],
        };
      }
      if (path.startsWith("/api/ops/tasks")) {
        return { items: [{ id: "task-1", status: "queued", phase: "validate" }] };
      }
      if (path === "/api/environments") {
        throw new ApiError({ status: 403, code: "FORBIDDEN", message: "not for DOM" });
      }
      if (path === "/api/llm/configs") return [];
      if (path === "/api/operation-plans?limit=20") {
        return { items: [{ id: "plan-1", status: "approval_pending" }] };
      }
      if (path === "/api/approvals?limit=20") {
        return { items: [{ id: "approval-1", plan_id: "plan-1", status: "pending" }] };
      }
      throw new Error(`unexpected path ${path}`);
    },
  };

  const projections = await loadDashboardProjections(client);

  assert.deepEqual(calls, [
    "/readyz",
    "/api/ops/tasks?limit=20",
    "/api/environments",
    "/api/llm/configs",
    "/api/operation-plans?limit=20",
    "/api/approvals?limit=20",
  ]);
  assert.equal(projections.health.status, "healthy");
  assert.equal(projections.tasks.status, "ready");
  assert.equal(projections.environments.status, "forbidden");
  assert.equal(projections.environments.errorCode, "FORBIDDEN");
  assert.equal(projections.models.status, "empty");
  assert.equal(projections.plans.status, "ready");
  assert.equal(projections.approvals.status, "ready");
});

test("approval actions bind plan hash and idempotency without surfacing errors", async () => {
  const calls = [];
  let refreshes = 0;
  const client = {
    async post(path, body, options) {
      calls.push({ path, body, options });
      if (path.endsWith("/reject")) {
        throw new ApiError({
          status: 500,
          code: "APPROVAL_STORE_DOWN",
          message: "db password should not render",
        });
      }
      return { ok: true };
    },
  };
  const actions = createApprovalActions({
    client,
    refresh: async () => { refreshes += 1; },
    idempotencyKeyFactory: () => "write-key",
  });
  const approval = { id: "approval-1", planHash: "b".repeat(64) };
  const plan = { id: "plan-1", planHash: "b".repeat(64) };

  assert.deepEqual(await actions.approve(approval), { status: "succeeded", errorCode: null });
  assert.deepEqual(await actions.execute(plan, approval), { status: "succeeded", errorCode: null });
  const rejected = await actions.reject(approval);
  assert.deepEqual(rejected, { status: "failed", errorCode: "APPROVAL_STORE_DOWN" });
  assert.equal(JSON.stringify(rejected).includes("db password"), false);
  assert.equal(calls[0].body.plan_hash, "b".repeat(64));
  assert.equal(calls[1].options.headers["Idempotency-Key"], "write-key");
  assert.equal(refreshes, 2);
});

test("selected task projection never surfaces server error messages", async () => {
  const client = {
    async get() {
      throw new ApiError({
        status: 500,
        code: "INTERNAL_ERROR",
        message: "provider key sk-should-not-render",
      });
    },
  };
  const projection = await loadTaskProjection(client, "task-1");
  assert.deepEqual(projection, {
    task: null,
    status: "error",
    errorCode: "INTERNAL_ERROR",
  });
  assert.equal(JSON.stringify(projection).includes("sk-should-not-render"), false);
  assert.equal(resourceStatus({ items: [], error: new ApiError({ status: 401 }) }), "forbidden");
});

test("onboarding progress is derived from safe tenant environment and model facts", () => {
  const base = {
    connections: {
      status: "ready",
      items: [
        {
          id: "env-k8s",
          name: "Primary Kubernetes",
          type: "kubernetes",
          provider: "kind",
          mode: "live",
          status: "available",
          credential_ref: "secret://tenant/kubernetes",
        },
      ],
    },
    models: {
      status: "ready",
      items: [
        {
          configId: "model-1",
          displayName: "Primary",
          provider: "openai",
          model: "gpt-test",
          hasApiKey: true,
          enabled: true,
          status: "available",
          maskedApiKey: "****cret",
        },
      ],
    },
    tasks: { status: "empty", items: [] },
  };

  const needsPrometheus = deriveOnboardingFacts(base);
  assert.equal(needsPrometheus.nextStep, "prometheus");
  assert.equal(needsPrometheus.kubernetes.primary.id, "env-k8s");
  assert.equal(JSON.stringify(needsPrometheus).includes("secret://"), false);
  assert.equal(JSON.stringify(needsPrometheus).includes("****cret"), false);

  const inspectionReady = deriveOnboardingFacts({ ...base, prometheusSkipped: true });
  assert.equal(inspectionReady.nextStep, "inspection");
  const completed = deriveOnboardingFacts({
    ...base,
    prometheusSkipped: true,
    tasks: {
      status: "ready",
      items: [{ id: "task-1", environmentId: "env-k8s", status: "succeeded", phase: "report" }],
    },
  });
  assert.equal(completed.completed, true);

  const ui = createOnboardingUiState();
  assert.equal(ui.getState().prometheusSkipped, false);
  ui.skipPrometheus();
  assert.equal(ui.getState().prometheusSkipped, true);
});

test("onboarding actions use save, test, and readonly task APIs without returning credentials", async () => {
  const calls = [];
  let refreshes = 0;
  const client = {
    async post(path, body, options) {
      calls.push({ path, body, options });
      if (path === "/api/environments") return { id: "env-1" };
      if (path === "/api/environments/env-1/test") return { status: "available" };
      if (path === "/api/llm/configs") return { config_id: "model-1", masked_api_key: "****cret" };
      if (path === "/api/llm/configs/model-1/test") return { success: true };
      if (path === "/api/ops/tasks") return { id: "task-1" };
      throw new Error(`unexpected path ${path}`);
    },
  };
  const actions = createOnboardingActions({
    client,
    refresh: async () => { refreshes += 1; },
    idempotencyKeyFactory: () => "onboarding-test-key",
  });

  assert.deepEqual(await actions.saveAndTestEnvironment({
    environmentType: "kubernetes",
    name: "Primary Kubernetes",
    provider: "kind",
    mode: "mock",
    namespace: "payments",
  }), { status: "succeeded", errorCode: null });
  const modelResult = await actions.saveAndTestModel({
    provider: "openai",
    displayName: "Primary",
    model: "gpt-test",
    apiKey: "sk-onboarding-secret",
  });
  assert.deepEqual(modelResult, { status: "succeeded", errorCode: null });
  assert.equal(JSON.stringify(modelResult).includes("sk-onboarding-secret"), false);
  assert.equal(JSON.stringify(modelResult).includes("****cret"), false);
  assert.deepEqual(await actions.runReadonlyInspection({ environmentId: "env-1" }), {
    status: "succeeded",
    errorCode: null,
  });
  assert.equal(calls.find((call) => call.path === "/api/ops/tasks").options.headers["Idempotency-Key"], "onboarding-test-key");
  assert.equal(calls.find((call) => call.path === "/api/environments").body.scope.namespaces[0], "payments");
  assert.equal(refreshes, 3);
});

test("onboarding action failures are generic and classify forbidden responses", async () => {
  const actions = createOnboardingActions({
    client: {
      async post() {
        throw new ApiError({
          status: 403,
          code: "FORBIDDEN",
          message: "credential sk-do-not-render",
        });
      },
    },
  });

  const result = await actions.testEnvironment("env-1");
  assert.deepEqual(result, { status: "forbidden", errorCode: "FORBIDDEN" });
  assert.equal(JSON.stringify(result).includes("sk-do-not-render"), false);
});
