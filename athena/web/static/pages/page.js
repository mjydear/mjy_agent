export function replacePage(container, { eyebrow, title, description }) {
  container.replaceChildren();
  const page = document.createElement("section");
  page.className = "module-page";

  if (eyebrow) {
    const label = document.createElement("p");
    label.className = "module-page-eyebrow";
    label.textContent = eyebrow;
    page.append(label);
  }
  const heading = document.createElement("h1");
  heading.textContent = title;
  const detail = document.createElement("p");
  detail.className = "module-page-description";
  detail.textContent = description;
  page.append(heading, detail);
  container.append(page);
  return page;
}

export function appendSection(page, title, content) {
  const section = document.createElement("section");
  section.className = "module-page-section";
  const heading = document.createElement("h2");
  heading.textContent = title;
  section.append(heading, content);
  page.append(section);
  return section;
}

export function createFactList(entries) {
  const list = document.createElement("ul");
  list.className = "module-fact-list";
  entries.forEach(({ label, value }) => {
    const row = document.createElement("li");
    const name = document.createElement("strong");
    const detail = document.createElement("span");
    name.textContent = label;
    detail.textContent = value || "-";
    row.append(name, detail);
    list.append(row);
  });
  return list;
}

export function createModeWatermark(mode) {
  const normalized = typeof mode === "string" ? mode.toLowerCase() : "";
  if (!["replay", "mock"].includes(normalized)) return null;
  const watermark = document.createElement("p");
  watermark.className = "module-mode-watermark";
  watermark.textContent = `${normalized.toUpperCase()} data - not live production evidence`;
  return watermark;
}
