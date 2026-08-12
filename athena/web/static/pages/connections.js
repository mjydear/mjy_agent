import { createEmptyState } from "../components/empty-state.js";
import { createProjectionState } from "../components/projection-state.js";
import { createStatusBadge } from "../components/status-badge.js";
import { appendSection, createModeWatermark, replacePage } from "./page.js";

export function renderConnections(container, { connections } = {}) {
  const page = replacePage(container, {
    eyebrow: "Tenant environments",
    title: "Connections",
    description: "Connection metadata is projected without credentials, references, or provider error details.",
  });
  const status = connections?.status || "idle";
  const items = Array.isArray(connections?.items) ? connections.items : [];
  if (status !== "ready") {
    appendSection(
      page,
      "Environments",
      createProjectionState(status, {
        emptyTitle: "No environments",
        emptyMessage: "Connect a Kubernetes or Prometheus environment to continue.",
      }),
    );
    return;
  }
  if (!items.length) {
    appendSection(
      page,
      "Environments",
      createEmptyState({ title: "No environments", message: "Connect a Kubernetes or Prometheus environment to continue." }),
    );
    return;
  }
  const list = document.createElement("ul");
  list.className = "module-connection-list";
  items.forEach((connection) => {
    const row = document.createElement("li");
    const details = document.createElement("div");
    const name = document.createElement("strong");
    const metadata = document.createElement("small");
    name.textContent = connection.name;
    metadata.textContent = `${connection.type} | ${connection.provider} | ${connection.capabilities.length} capabilities`;
    details.append(name, metadata);
    row.append(details, createStatusBadge(connection.status), createStatusBadge(connection.mode));
    list.append(row);
  });
  appendSection(page, "Environments", list);
  const nonLive = items.find((connection) => ["replay", "mock"].includes(connection.mode));
  const watermark = createModeWatermark(nonLive?.mode);
  if (watermark) page.append(watermark);
}
