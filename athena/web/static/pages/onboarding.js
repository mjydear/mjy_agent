import { ONBOARDING_STEPS, isProjectionUsable } from "../core/onboarding.js";
import { createProjectionState } from "../components/projection-state.js";
import { createStatusBadge } from "../components/status-badge.js";
import { appendSection, replacePage } from "./page.js";

function actionButton(label, handler, { secondary = false, disabled = false } = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = secondary ? "module-action-button module-action-button--secondary" : "module-action-button";
  button.textContent = label;
  button.disabled = disabled;
  if (typeof handler === "function") {
    button.addEventListener("click", () => {
      Promise.resolve(handler()).catch(() => undefined);
    });
  }
  return button;
}

function invoke(action, payload) {
  if (typeof action !== "function") return Promise.resolve();
  return Promise.resolve(action(payload));
}

function appendActionFeedback(section, action, step, successMessage) {
  if (action?.step !== step || action.status === "idle") return;
  if (action.status === "loading") {
    const progress = document.createElement("p");
    progress.className = "module-onboarding-feedback";
    progress.append(createStatusBadge("loading"), document.createTextNode(" Saving and testing."));
    section.append(progress);
    return;
  }
  if (["error", "forbidden"].includes(action.status)) {
    const state = createProjectionState(action.status);
    if (action.errorCode) state.dataset.errorCode = action.errorCode;
    section.append(state);
    return;
  }
  if (action.status === "succeeded") {
    const success = document.createElement("p");
    success.className = "module-onboarding-feedback";
    success.append(createStatusBadge("succeeded"), document.createTextNode(` ${successMessage}`));
    section.append(success);
  }
}

function createProgress(facts) {
  const list = document.createElement("ol");
  list.className = "module-onboarding-progress";
  ONBOARDING_STEPS.forEach((step, index) => {
    const item = document.createElement("li");
    const label = document.createElement("span");
    const current = facts.nextStep === step.id;
    const completed = step.id === "kubernetes"
      ? facts.kubernetes.state === "complete"
      : step.id === "llm"
        ? facts.llm.state === "complete"
        : step.id === "prometheus"
          ? facts.prometheus.state === "complete" || facts.prometheus.skipped
          : facts.inspection.state === "complete";
    item.dataset.current = String(current);
    item.dataset.complete = String(completed);
    label.textContent = `${index + 1}. ${step.label}`;
    item.append(label, createStatusBadge(completed ? "succeeded" : current ? "loading" : "unknown"));
    list.append(item);
  });
  return list;
}

function addTextField(form, { name, label, value = "", maxLength = 120, type = "text" }) {
  const field = document.createElement("label");
  field.className = "module-onboarding-field";
  const caption = document.createElement("span");
  caption.textContent = label;
  const input = document.createElement("input");
  input.name = name;
  input.type = type;
  input.value = value;
  input.maxLength = maxLength;
  input.required = true;
  input.autocomplete = type === "password" ? "new-password" : "off";
  field.append(caption, input);
  form.append(field);
  return input;
}

function addSelectField(form, { name, label, options, value }) {
  const field = document.createElement("label");
  field.className = "module-onboarding-field";
  const caption = document.createElement("span");
  caption.textContent = label;
  const select = document.createElement("select");
  select.name = name;
  options.forEach(({ value: optionValue, label: optionLabel }) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = optionLabel;
    option.selected = optionValue === value;
    select.append(option);
  });
  field.append(caption, select);
  form.append(field);
  return select;
}

function formValue(form, name) {
  return form.elements.namedItem(name)?.value || "";
}

function createEnvironmentForm(type, actions) {
  const form = document.createElement("form");
  form.className = "module-onboarding-form";
  addTextField(form, {
    name: "name",
    label: "Connection name",
    value: type === "kubernetes" ? "Primary Kubernetes" : "Prometheus",
  });
  addTextField(form, {
    name: "provider",
    label: "Provider",
    value: type === "kubernetes" ? "kubernetes" : "prometheus",
    maxLength: 80,
  });
  addSelectField(form, {
    name: "mode",
    label: "Evidence mode",
    value: "live",
    options: [
      { value: "live", label: "Live" },
      { value: "replay", label: "Replay" },
      { value: "mock", label: "Mock" },
    ],
  });
  addTextField(form, {
    name: "namespace",
    label: "Default namespace",
    value: "default",
  });
  const submit = document.createElement("button");
  submit.className = "module-action-button";
  submit.type = "submit";
  submit.textContent = "Save and test";
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    invoke(actions?.saveAndTestEnvironment, {
      environmentType: type,
      name: formValue(form, "name"),
      provider: formValue(form, "provider"),
      mode: formValue(form, "mode"),
      namespace: formValue(form, "namespace"),
    }).catch(() => undefined);
  });
  return form;
}

function createModelForm(actions) {
  const form = document.createElement("form");
  form.className = "module-onboarding-form";
  addSelectField(form, {
    name: "provider",
    label: "Provider",
    value: "openai",
    options: [
      { value: "openai", label: "OpenAI" },
      { value: "anthropic", label: "Anthropic" },
      { value: "deepseek", label: "DeepSeek" },
      { value: "gemini", label: "Google Gemini" },
      { value: "dashscope", label: "DashScope" },
    ],
  });
  addTextField(form, { name: "displayName", label: "Display name", value: "Primary model", maxLength: 80 });
  addTextField(form, { name: "model", label: "Model", value: "gpt-4o-mini", maxLength: 120 });
  const apiKey = addTextField(form, {
    name: "apiKey",
    label: "API key",
    value: "",
    maxLength: 300,
    type: "password",
  });
  const submit = document.createElement("button");
  submit.className = "module-action-button";
  submit.type = "submit";
  submit.textContent = "Save and test";
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const credential = apiKey.value;
    // Do not retain the credential in the DOM or in the in-memory projection.
    apiKey.value = "";
    invoke(actions?.saveAndTestModel, {
      provider: formValue(form, "provider"),
      displayName: formValue(form, "displayName"),
      model: formValue(form, "model"),
      apiKey: credential,
    }).catch(() => undefined);
  });
  return form;
}

function renderKubernetesStep(page, facts, action, actions) {
  const content = document.createElement("div");
  const section = appendSection(page, "Connect Kubernetes", content);
  if (!isProjectionUsable(facts.kubernetes.resourceStatus)) {
    content.append(createProjectionState(facts.kubernetes.resourceStatus));
    return;
  }
  if (!facts.kubernetes.configured) {
    content.append(createEnvironmentForm("kubernetes", actions));
  } else if (!facts.kubernetes.available) {
    const detail = document.createElement("p");
    detail.textContent = `${facts.kubernetes.primary.name} is configured but has not passed a connection test.`;
    content.append(detail, actionButton("Test connection", () => invoke(
      actions?.testEnvironment,
      { environmentId: facts.kubernetes.primary.id, step: "kubernetes" },
    )));
  }
  appendActionFeedback(content, action, "kubernetes", "Connection test completed.");
}

function renderLlmStep(page, facts, action, actions) {
  const content = document.createElement("div");
  const section = appendSection(page, "Configure LLM", content);
  if (!isProjectionUsable(facts.llm.resourceStatus)) {
    content.append(createProjectionState(facts.llm.resourceStatus));
    return;
  }
  if (!facts.llm.configured) {
    content.append(createModelForm(actions));
  } else if (!facts.llm.available) {
    const detail = document.createElement("p");
    detail.textContent = `${facts.llm.primary.displayName} is configured but has not passed a model connection test.`;
    content.append(detail, actionButton("Test model", () => invoke(
      actions?.testModel,
      facts.llm.primary.configId,
    )));
  }
  appendActionFeedback(content, action, "llm", "Model connection test completed.");
}

function renderPrometheusStep(page, facts, action, actions) {
  const content = document.createElement("div");
  const section = appendSection(page, "Connect Prometheus", content);
  if (!isProjectionUsable(facts.prometheus.resourceStatus)) {
    content.append(createProjectionState(facts.prometheus.resourceStatus));
    return;
  }
  if (!facts.prometheus.configured) {
    const controls = document.createElement("div");
    controls.className = "module-onboarding-controls";
    controls.append(
      actionButton("Skip", () => invoke(actions?.skipPrometheus), { secondary: true }),
    );
    content.append(createEnvironmentForm("prometheus", actions), controls);
  } else if (!facts.prometheus.available) {
    const detail = document.createElement("p");
    detail.textContent = `${facts.prometheus.primary.name} is configured but has not passed a connection test.`;
    content.append(detail, actionButton("Test connection", () => invoke(
      actions?.testEnvironment,
      { environmentId: facts.prometheus.primary.id, step: "prometheus" },
    )));
  }
  appendActionFeedback(content, action, "prometheus", "Connection test completed.");
}

function renderInspectionStep(page, facts, action, actions) {
  const content = document.createElement("div");
  const section = appendSection(page, "Run readonly inspection", content);
  if (!isProjectionUsable(facts.inspection.resourceStatus)) {
    content.append(createProjectionState(facts.inspection.resourceStatus));
    return;
  }
  if (facts.inspection.state === "running") {
    const detail = document.createElement("p");
    detail.textContent = `Readonly inspection ${facts.inspection.task.id} is ${facts.inspection.task.status}.`;
    content.append(detail, actionButton("View operation", () => invoke(
      actions?.viewInspection,
      facts.inspection.task.id,
    ), { secondary: true }));
  } else {
    const detail = document.createElement("p");
    detail.textContent = "The first inspection uses the readonly CloudOps workflow and creates a tenant-scoped OpsTask.";
    content.append(detail, actionButton("Run readonly inspection", () => invoke(
      actions?.runReadonlyInspection,
      { environmentId: facts.inspection.environment?.id, namespace: "default" },
    )));
  }
  appendActionFeedback(content, action, "inspection", "Readonly inspection started.");
}

function renderCompletion(page, facts, actions) {
  const content = document.createElement("div");
  appendSection(page, "Ready for the console", content);
  const message = document.createElement("p");
  message.textContent = `Kubernetes, model configuration, and the first readonly inspection are complete for this tenant.${facts.prometheus.skipped ? " Prometheus was skipped for this browser session." : ""}`;
  content.append(message, actionButton("Enter console", () => invoke(actions?.enterConsole)));
}

export function renderOnboarding(container, {
  onboarding: facts,
  onboardingAction: action,
  onboardingActions: actions,
} = {}) {
  const page = replacePage(container, {
    eyebrow: "Tenant setup",
    title: "CloudOps onboarding",
    description: "Connection and model facts are loaded from tenant-scoped APIs. Completion is rebuilt from those facts whenever the page refreshes.",
  });
  if (!facts) {
    appendSection(page, "Setup", createProjectionState("loading"));
    return;
  }
  appendSection(page, "Setup progress", createProgress(facts));
  if (facts.completed) {
    renderCompletion(page, facts, actions);
    return;
  }
  if (facts.nextStep === "kubernetes") renderKubernetesStep(page, facts, action, actions);
  else if (facts.nextStep === "llm") renderLlmStep(page, facts, action, actions);
  else if (facts.nextStep === "prometheus") renderPrometheusStep(page, facts, action, actions);
  else renderInspectionStep(page, facts, action, actions);
}
