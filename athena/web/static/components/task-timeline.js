import { createEmptyState } from "./empty-state.js";

function eventLabel(event) {
  return typeof event?.event_type === "string" ? event.event_type : "task.event";
}

/** Render only task lifecycle metadata; event payloads stay out of the DOM. */
export function createTaskTimeline(events = []) {
  if (!Array.isArray(events) || events.length === 0) {
    return createEmptyState({
      title: "No task events",
      message: "Task lifecycle events will appear after the API projection is loaded.",
    });
  }

  const list = document.createElement("ol");
  list.className = "module-task-timeline";
  events.forEach((event) => {
    const item = document.createElement("li");
    const name = document.createElement("strong");
    const timestamp = document.createElement("time");
    name.textContent = eventLabel(event);
    timestamp.textContent = typeof event?.created_at === "string" ? event.created_at : "";
    item.append(name, timestamp);
    list.append(item);
  });
  return list;
}
