import { createEmptyState } from "../components/empty-state.js";
import { appendSection, replacePage } from "./page.js";

export function renderAudit(container) {
  const page = replacePage(container, {
    eyebrow: "Governance",
    title: "Audit",
    description: "Audit records remain server-authoritative and are never rebuilt from browser cache.",
  });
  appendSection(
    page,
    "Audit events",
    createEmptyState({ title: "No audit projection loaded", message: "The audit route is prepared for its API-backed page." }),
  );
}
