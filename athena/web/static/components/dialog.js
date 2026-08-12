export function createDialog({ title, description }) {
  const dialog = document.createElement("dialog");
  dialog.className = "module-dialog";

  const heading = document.createElement("h2");
  heading.textContent = title;
  const detail = document.createElement("p");
  detail.textContent = description;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "module-dialog-close";
  close.textContent = "Close";
  close.addEventListener("click", () => dialog.close());

  dialog.append(heading, detail, close);
  return dialog;
}

export function openDialog(dialog) {
  if (!(dialog instanceof HTMLDialogElement)) throw new TypeError("A dialog is required");
  if (!dialog.open) dialog.showModal();
}
