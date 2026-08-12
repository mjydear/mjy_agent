import { createEmptyState } from "../components/empty-state.js";
import { createProjectionState } from "../components/projection-state.js";
import { createStatusBadge } from "../components/status-badge.js";
import { appendSection, replacePage } from "./page.js";

export function renderModelSettings(container, { models } = {}) {
  const page = replacePage(container, {
    eyebrow: "Model settings",
    title: "Models",
    description: "Only model metadata is shown here. Credential values, masked values, and endpoints never enter this page.",
  });
  const status = models?.status || "idle";
  const items = Array.isArray(models?.items) ? models.items : [];
  if (status !== "ready") {
    appendSection(
      page,
      "Configured models",
      createProjectionState(status, {
        emptyTitle: "No model configuration",
        emptyMessage: "Use the existing server-managed settings control to add a model.",
      }),
    );
    return;
  }
  if (!items.length) {
    appendSection(
      page,
      "Configured models",
      createEmptyState({ title: "No model configuration", message: "Use the existing server-managed settings control to add a model." }),
    );
    return;
  }
  const list = document.createElement("ul");
  list.className = "module-model-list";
  items.forEach((item) => {
    const row = document.createElement("li");
    const details = document.createElement("div");
    const name = document.createElement("strong");
    const metadata = document.createElement("small");
    name.textContent = item.displayName;
    metadata.textContent = `${item.provider} | ${item.model}${item.isDefault ? " | default" : ""}`;
    details.append(name, metadata);
    row.append(details, createStatusBadge(item.status));
    list.append(row);
  });
  appendSection(
    page,
    "Configured models",
    list,
  );
}
