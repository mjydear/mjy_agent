import { createEmptyState } from "./empty-state.js";

/**
 * Evidence content is intentionally excluded. This projection exposes only
 * metadata that users can use to assess source, origin, and collection time.
 */
export function createEvidencePanel(evidence = []) {
  if (!Array.isArray(evidence) || evidence.length === 0) {
    return createEmptyState({
      title: "No evidence metadata",
      message: "Evidence metadata will appear after a task is selected.",
    });
  }

  const list = document.createElement("ul");
  list.className = "module-evidence-panel";
  evidence.forEach((item) => {
    const row = document.createElement("li");
    const source = document.createElement("strong");
    const detail = document.createElement("span");
    source.textContent = typeof item?.source === "string" ? item.source : "unknown source";
    const origin = typeof item?.data_origin === "string" ? item.data_origin : "unknown";
    const observedAt = typeof item?.observed_at === "string" ? item.observed_at : "";
    detail.textContent = observedAt ? `${origin} | ${observedAt}` : origin;
    row.append(source, detail);
    list.append(row);
  });
  return list;
}
