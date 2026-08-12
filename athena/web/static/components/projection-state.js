import { createEmptyState } from "./empty-state.js";
import { createStatusBadge } from "./status-badge.js";

const COPY = Object.freeze({
  idle: ["Loading data", "This projection will be requested when the module preview starts."],
  loading: ["Loading data", "Retrieving the latest tenant-scoped projection."],
  empty: ["No records", "No records are currently available for this tenant."],
  forbidden: ["Access restricted", "Your current role cannot view this projection."],
  error: ["Data unavailable", "The projection could not be loaded. Retry after the service recovers."],
  degraded: ["Degraded data", "The service reported a degraded dependency. Treat this projection as partial."],
});

/** Render generic, non-sensitive request state. Server error messages stay out of the DOM. */
export function createProjectionState(status, { emptyTitle, emptyMessage } = {}) {
  const normalized = COPY[status] ? status : "error";
  const [defaultTitle, defaultMessage] = COPY[normalized];
  const state = createEmptyState({
    title: normalized === "empty" ? emptyTitle || defaultTitle : defaultTitle,
    message: normalized === "empty" ? emptyMessage || defaultMessage : defaultMessage,
  });
  state.dataset.projectionState = normalized;
  state.prepend(createStatusBadge(normalized));
  return state;
}
