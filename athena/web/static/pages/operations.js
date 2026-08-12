import { createEvidencePanel } from "../components/evidence-panel.js";
import { createProjectionState } from "../components/projection-state.js";
import { createStatusBadge } from "../components/status-badge.js";
import { createTaskTimeline } from "../components/task-timeline.js";
import { appendSection, createFactList, createModeWatermark, replacePage } from "./page.js";

function recentTasks(tasks) {
  const list = document.createElement("ul");
  list.className = "module-projection-list";
  tasks.forEach((task) => {
    const row = document.createElement("li");
    const details = document.createElement("div");
    const name = document.createElement("strong");
    const metadata = document.createElement("small");
    name.textContent = `Task ${task.id}`;
    metadata.textContent = `${task.phase} | ${task.environmentMode}`;
    details.append(name, metadata);
    row.append(details, createStatusBadge(task.status));
    list.append(row);
  });
  return list;
}

export function renderOperations(container, { task, taskResource, tasks, events = [], evidence = [] } = {}) {
  const routeTask = taskResource?.selected === true;
  const taskStatus = routeTask ? taskResource.status : tasks?.status || "idle";
  const title = task?.id ? `Task ${task.id}` : "Operations";
  const page = replacePage(container, {
    eyebrow: "Readonly operations",
    title,
    description: "Lifecycle metadata is server-authoritative. The P1 workbench remains the default compatible task surface.",
  });
  if (taskStatus !== "ready") {
    appendSection(
      page,
      "Task projection",
      createProjectionState(taskStatus, {
        emptyTitle: "No tasks",
        emptyMessage: "Create a readonly task after connecting an environment.",
      }),
    );
    return;
  }
  if (!task && !routeTask) {
    if (!Array.isArray(tasks?.items) || !tasks.items.length) {
      appendSection(
        page,
        "Task projection",
        createEmptyState({ title: "No tasks", message: "Create a readonly task after connecting an environment." }),
      );
      return;
    }
    appendSection(page, "Recent tasks", recentTasks(tasks.items));
    return;
  }
  if (!task) {
    appendSection(
      page,
      "Task projection",
      createProjectionState("error"),
    );
    return;
  }
  const facts = createFactList([
    { label: "Status", value: task.status },
    { label: "Phase", value: task.phase },
    { label: "Environment", value: task.environmentId },
    { label: "Mode", value: task.environmentMode },
    { label: "Event count", value: task.eventCount === null ? "-" : String(task.eventCount) },
    { label: "Evidence count", value: task.evidenceCount === null ? "-" : String(task.evidenceCount) },
  ]);
  appendSection(page, "Task facts", facts);
  if (task.degraded) {
    const notice = document.createElement("p");
    notice.className = "module-degraded-notice";
    notice.textContent = "This task is degraded. Review the final server-side task report before using its result.";
    appendSection(page, "Degradation", notice);
  }
  const watermark = createModeWatermark(task.environmentMode);
  if (watermark) page.append(watermark);
  appendSection(page, "Task timeline", createTaskTimeline(events));
  appendSection(page, "Evidence metadata", createEvidencePanel(evidence));
}
