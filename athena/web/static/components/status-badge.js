const STATUS_LABELS = Object.freeze({
  idle: "Idle",
  loading: "Loading",
  available: "Available",
  healthy: "Healthy",
  degraded: "Degraded",
  unavailable: "Unavailable",
  forbidden: "Access restricted",
  error: "Unavailable",
  live: "Live",
  replay: "Replay",
  mock: "Mock",
  unknown: "Unknown",
  queued: "Queued",
  running: "Running",
  waiting: "Waiting",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
});

export function createStatusBadge(status, { label } = {}) {
  const rawValue = typeof status === "string" ? status : "unknown";
  const value = /^[a-z0-9_-]+$/i.test(rawValue) ? rawValue.toLowerCase() : "unknown";
  const badge = document.createElement("span");
  badge.className = `module-status-badge module-status-badge--${value}`;
  badge.textContent = label || STATUS_LABELS[value] || STATUS_LABELS.unknown;
  return badge;
}
