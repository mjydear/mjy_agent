import { createEmptyState } from "../components/empty-state.js";
import { appendSection, replacePage } from "./page.js";

export function renderAlerts(container) {
  const page = replacePage(container, {
    eyebrow: "Incident intake",
    title: "Alerts",
    description: "Alert facts will be rendered from tenant-scoped API projections.",
  });
  appendSection(
    page,
    "Alert history",
    createEmptyState({ title: "No alert projection loaded", message: "The page is ready for the alert route migration." }),
  );
}
