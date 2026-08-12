import { ApiError } from "./api.js";

function publicErrorCode(error) {
  if (error instanceof ApiError && typeof error.code === "string") return error.code;
  return "APPROVAL_REQUEST_FAILED";
}

function actionResult(status, errorCode = null) {
  return Object.freeze({ status, errorCode });
}

async function runAction(operation, refresh) {
  try {
    await operation();
    if (typeof refresh === "function") await refresh();
    return actionResult("succeeded");
  } catch (error) {
    const status = error instanceof ApiError && [401, 403].includes(error.status)
      ? "forbidden"
      : "failed";
    return actionResult(status, publicErrorCode(error));
  }
}

export function createApprovalActions({
  client,
  refresh,
  idempotencyKeyFactory = () => `approval-${Date.now()}`,
} = {}) {
  if (!client) throw new TypeError("An API client is required");

  return Object.freeze({
    approve: (approval) => runAction(
      () => client.post(
        `/api/approvals/${encodeURIComponent(approval.id)}/approve`,
        { plan_hash: approval.planHash },
      ),
      refresh,
    ),
    reject: (approval) => runAction(
      () => client.post(
        `/api/approvals/${encodeURIComponent(approval.id)}/reject`,
        { note: "Rejected from approval workbench" },
      ),
      refresh,
    ),
    execute: (plan, approval) => runAction(
      () => client.post(
        `/api/operation-plans/${encodeURIComponent(plan.id)}/execute`,
        { approval_id: approval.id, plan_hash: plan.planHash },
        { headers: { "Idempotency-Key": idempotencyKeyFactory(plan, approval) } },
      ),
      refresh,
    ),
  });
}
