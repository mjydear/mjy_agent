import { createEmptyState } from "../components/empty-state.js";
import { createProjectionState } from "../components/projection-state.js";
import { createStatusBadge } from "../components/status-badge.js";
import { appendSection, replacePage } from "./page.js";

function createActionButton(label, handler, { secondary = false, disabled = false } = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = secondary
    ? "module-action-button module-action-button--secondary"
    : "module-action-button";
  button.textContent = label;
  button.disabled = disabled;
  if (typeof handler === "function" && !disabled) button.addEventListener("click", handler);
  return button;
}

function shortHash(value) {
  return typeof value === "string" && value.length > 12
    ? `${value.slice(0, 8)}...${value.slice(-6)}`
    : value || "-";
}

function findPlan(plans, approval) {
  return (plans || []).find((plan) => plan.id === approval.planId) || null;
}

function approvalRows({ approvals, plans, actions }) {
  if (!approvals.length) {
    return createEmptyState({
      title: "No approvals",
      message: "Writable changes appear here only after an immutable plan requests approval.",
    });
  }
  const list = document.createElement("ul");
  list.className = "module-approval-list";
  approvals.forEach((approval) => {
    const plan = findPlan(plans, approval);
    const row = document.createElement("li");
    const details = document.createElement("div");
    const title = document.createElement("strong");
    const meta = document.createElement("small");
    title.textContent = plan
      ? `${plan.actionType} ${plan.resourceKind}/${plan.resourceName}`
      : `Plan ${approval.planId}`;
    meta.textContent = `${plan?.namespace || "-"} | ${plan?.riskLevel || "-"} | ${shortHash(approval.planHash)}`;
    details.append(title, meta);

    const controls = document.createElement("div");
    controls.className = "module-approval-controls";
    controls.append(createStatusBadge(approval.status));
    if (approval.status === "pending") {
      controls.append(
        createActionButton("Approve", () => actions?.approve?.(approval)),
        createActionButton("Reject", () => actions?.reject?.(approval), { secondary: true }),
      );
    }
    if (approval.status === "approved" && plan?.status === "approved") {
      controls.append(createActionButton("Execute", () => actions?.execute?.(plan, approval)));
    }
    row.append(details, controls);
    list.append(row);
  });
  return list;
}

function planRows(plans) {
  if (!plans.length) {
    return createEmptyState({
      title: "No operation plans",
      message: "Readonly diagnosis remains available without write plans.",
    });
  }
  const list = document.createElement("ul");
  list.className = "module-approval-list";
  plans.forEach((plan) => {
    const row = document.createElement("li");
    const details = document.createElement("div");
    const title = document.createElement("strong");
    const meta = document.createElement("small");
    title.textContent = `${plan.actionType} ${plan.resourceKind}/${plan.resourceName}`;
    meta.textContent = `${plan.namespace} | ${plan.requiredScope} | ${shortHash(plan.planHash)}`;
    details.append(title, meta);
    row.append(details, createStatusBadge(plan.status));
    list.append(row);
  });
  return list;
}

function renderResource(resource, renderItems, emptyTitle, emptyMessage) {
  const status = resource?.status || "idle";
  if (status !== "ready") {
    return createProjectionState(status, { emptyTitle, emptyMessage });
  }
  return renderItems(resource.items || []);
}

export function renderApprovals(container, {
  approvals,
  plans,
  approvalActions,
} = {}) {
  const page = replacePage(container, {
    eyebrow: "Governance",
    title: "Approvals",
    description: "Writable actions require immutable plans, matching hashes, approval, execution scope, and idempotent ToolEffect records.",
  });
  appendSection(
    page,
    "Approval queue",
    renderResource(
      approvals,
      (items) => approvalRows({ approvals: items, plans: plans?.items || [], actions: approvalActions }),
      "No approval queue",
      "No operation plan is waiting for approval.",
    ),
  );
  appendSection(
    page,
    "Operation plans",
    renderResource(
      plans,
      planRows,
      "No plans",
      "Create an OperationPlan from a controlled write proposal.",
    ),
  );
}
