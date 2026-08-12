export function createEmptyState({ title, message }) {
  const section = document.createElement("section");
  section.className = "module-empty-state";

  const heading = document.createElement("h2");
  heading.textContent = title;
  const detail = document.createElement("p");
  detail.textContent = message;

  section.append(heading, detail);
  return section;
}
