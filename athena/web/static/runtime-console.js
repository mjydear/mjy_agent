import { mountSkillEvaluation } from "./pages/skill-evaluation.js";

const RUNTIME_ROOT = "/api/runtime/tasks";
const POLL_INTERVAL_MS = 1800;
const TERMINAL_STATUSES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "budget_exhausted",
]);
const PUBLIC_EVENT_FIELD = /thought|reasoning|chain[_-]?of[_-]?thought|scratchpad/i;
const STATUS_LABELS = Object.freeze({
  queued: "排队中",
  running: "运行中",
  waiting_human: "等待输入",
  waiting: "等待输入",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  budget_exhausted: "预算耗尽",
  unknown: "未知",
});
const EVENT_LABELS = Object.freeze({
  "task.created": "任务已创建",
  "task.resumed": "任务已恢复",
  "task.cancel_requested": "已请求取消",
  "task.cancelled": "任务已取消",
  "tick.started": "ReAct Tick 已开始",
  "tick.completed": "ReAct Tick 已提交",
  "tool.called": "工具调用",
  "tool.succeeded": "工具结果",
  "tool.rejected": "工具已拒绝",
  "task.waiting_human": "等待人工输入",
  "task.succeeded": "任务已完成",
  "task.failed": "任务失败",
  "task.budget_exhausted": "预算耗尽",
  task_created: "任务已创建",
  task_started: "任务开始执行",
  tick_committed: "ReAct Tick 已提交",
  decision: "决策已记录",
  tool_call: "工具调用",
  tool_result: "工具结果",
  evidence_created: "证据已沉淀",
  context_compacted: "上下文已压缩",
  usage_recorded: "用量已结算",
  task_waiting_human: "等待人工输入",
  task_succeeded: "任务已完成",
  task_failed: "任务失败",
  task_cancelled: "任务已取消",
});

function requireMountArguments(host, options) {
  if (!host || typeof host.replaceChildren !== "function") {
    throw new TypeError("mountRuntimeConsole requires a DOM root element");
  }
  const client = options?.api;
  if (!client || typeof client.get !== "function" || typeof client.post !== "function") {
    throw new TypeError("mountRuntimeConsole requires options.api with get and post methods");
  }
  return client;
}

function createElement(tagName, options = {}, children = []) {
  const element = document.createElement(tagName);
  if (options.className) element.className = options.className;
  if (options.text !== undefined && options.text !== null) element.textContent = String(options.text);
  if (options.type) element.type = options.type;
  if (options.id) element.id = options.id;
  if (options.name) element.name = options.name;
  if (options.htmlFor) element.htmlFor = options.htmlFor;
  if (options.value !== undefined) element.value = options.value;
  if (options.placeholder) element.placeholder = options.placeholder;
  if (options.title) element.title = options.title;
  if (options.role) element.setAttribute("role", options.role);
  if (options.ariaLabel) element.setAttribute("aria-label", options.ariaLabel);
  if (options.ariaLive) element.setAttribute("aria-live", options.ariaLive);
  if (options.ariaSelected !== undefined) element.setAttribute("aria-selected", String(options.ariaSelected));
  if (options.disabled) element.disabled = true;
  if (options.hidden) element.hidden = true;
  if (options.dataset) {
    Object.entries(options.dataset).forEach(([key, value]) => {
      if (value !== undefined && value !== null) element.dataset[key] = String(value);
    });
  }
  const values = Array.isArray(children) ? children : [children];
  values.filter(Boolean).forEach((child) => element.append(child));
  return element;
}

function createButton(label, { action, taskId, variant = "secondary", disabled = false, title, type = "button" } = {}) {
  return createElement("button", {
    className: `runtime-console__button runtime-console__button--${variant}`,
    text: label,
    type,
    title,
    disabled,
    dataset: { action, taskId },
  });
}

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asItems(payload, keys = ["items", "tasks", "events", "data"]) {
  if (Array.isArray(payload)) return payload;
  const record = asObject(payload);
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key];
  }
  return [];
}

function taskIdOf(task) {
  return String(task?.task_id || task?.id || "");
}

function taskProjection(payload) {
  const record = asObject(payload);
  return asObject(record.task).task_id || asObject(record.task).id ? record.task : payload;
}

function taskGoalOf(task) {
  return String(task?.goal || task?.objective || task?.title || "未命名任务");
}

function taskStatusOf(task) {
  return String(task?.status || task?.state || "unknown").toLowerCase();
}

function isTerminal(task) {
  return TERMINAL_STATUSES.has(taskStatusOf(task));
}

function statusLabel(status) {
  const normalized = String(status || "unknown").toLowerCase();
  return STATUS_LABELS[normalized] || normalized.replaceAll("_", " ");
}

function statusTone(status) {
  const normalized = String(status || "unknown").toLowerCase();
  if (normalized === "succeeded") return "success";
  if (["failed", "cancelled", "budget_exhausted"].includes(normalized)) return "danger";
  if (["running", "queued", "waiting", "waiting_human"].includes(normalized)) return "progress";
  return "neutral";
}

function formatTime(value) {
  if (!value) return "时间未提供";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    month: "numeric",
    day: "numeric",
  }).format(parsed);
}

function formatNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? new Intl.NumberFormat("zh-CN").format(numeric) : "-";
}

function compactText(value, maxLength = 180) {
  const text = typeof value === "string" ? value.trim() : "";
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
}

function publicProjection(value) {
  if (Array.isArray(value)) return value.map(publicProjection);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !PUBLIC_EVENT_FIELD.test(key))
      .map(([key, item]) => [key, publicProjection(item)]),
  );
}

function publicJson(value) {
  try {
    return JSON.stringify(publicProjection(value), null, 2);
  } catch {
    return "无法展示此公共投影";
  }
}

function eventIdOf(event) {
  return String(event?.event_id || event?.id || event?.sequence || event?.seq || "");
}

function eventCursorOf(payload, events, fallback) {
  const record = asObject(payload);
  const cursor = record.next_cursor ?? record.cursor ?? record.nextCursor;
  if (cursor !== undefined && cursor !== null) return String(cursor);
  const lastEvent = events.at(-1);
  return eventIdOf(lastEvent) || fallback;
}

function eventTypeOf(event) {
  return String(event?.type || event?.event_type || event?.name || "event");
}

function eventDataOf(event) {
  return asObject(event?.data || event?.payload || event?.public_payload);
}

function eventSummary(event) {
  const data = eventDataOf(event);
  const candidates = [data.summary, data.message, data.status, data.tool_name, data.decision];
  const value = candidates.find((candidate) => typeof candidate === "string" && candidate.trim());
  return compactText(value || "已记录公开运行事件");
}

function evidenceSourceOf(evidence) {
  const source = evidence?.source_ref || evidence?.source || evidence?.artifact_id || evidence?.artifact?.id;
  return source ? String(source) : "来源未提供";
}

function reportOf(task) {
  const report = task?.report || task?.final_report || task?.result?.report || task?.result?.final_answer;
  if (typeof report === "string") return report.trim();
  if (report && typeof report === "object") return publicJson(report);
  return "";
}

function usageEntriesOf(usage) {
  return asItems(usage, ["items", "entries", "ledger", "usage"]);
}

function usageTotalOf(usage, entries) {
  const record = asObject(usage);
  const direct = record.total_tokens ?? record.total_token_count ?? record.tokens;
  if (Number.isFinite(Number(direct))) return Number(direct);
  return entries.reduce((total, entry) => total + Number(entry.total_tokens ?? entry.tokens ?? 0), 0);
}

function appendStatus(parent, status) {
  parent.append(createElement("span", {
    className: `runtime-console__status runtime-console__status--${statusTone(status)}`,
    text: statusLabel(status),
  }));
}

function createEmptyState(title, detail) {
  return createElement("section", { className: "runtime-console__empty" }, [
    createElement("h2", { text: title }),
    createElement("p", { text: detail }),
  ]);
}

function createErrorState(message, onRetry) {
  const retry = createButton("重试", { action: onRetry, variant: "secondary" });
  return createElement("section", { className: "runtime-console__error", role: "alert" }, [
    createElement("strong", { text: "请求未完成" }),
    createElement("p", { text: message }),
    retry,
  ]);
}

function createJsonBlock(value, emptyMessage) {
  const hasValue = value && (typeof value !== "object" || Object.keys(value).length > 0);
  if (!hasValue) return createEmptyState("暂无数据", emptyMessage);
  return createElement("pre", { className: "runtime-console__code", text: publicJson(value) });
}

/**
 * Mount the Runtime Console into an existing application shell.
 *
 * The caller supplies a host node and an options object containing the standard
 * API client. The module owns task projection loading, polling, selection,
 * forms, and inspector state.
 */
export function mountRuntimeConsole(host, options) {
  const root = host;
  const api = requireMountArguments(host, options);

  const state = {
    destroyed: false,
    tasks: [],
    selectedTaskId: null,
    task: null,
    events: [],
    eventCursor: null,
    evidence: [],
    context: null,
    usage: null,
    activeInspector: ["run", "context", "evidence", "usage"].includes(options.initialInspector)
      ? options.initialInspector
      : "run",
    listStatus: "loading",
    detailStatus: "idle",
    inspectorStatus: "idle",
    error: null,
    busyAction: null,
    requestVersion: 0,
    pollTimer: null,
    activeView: options.initialView === "skills" ? "skills" : "tasks",
    skillController: null,
  };

  function selectedTask() {
    return state.task || state.tasks.find((task) => taskIdOf(task) === state.selectedTaskId) || null;
  }

  function currentTaskPath(suffix = "") {
    return `${RUNTIME_ROOT}/${encodeURIComponent(state.selectedTaskId)}${suffix}`;
  }

  function setPolling() {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    const task = selectedTask();
    if (state.destroyed || !task || isTerminal(task)) return;
    state.pollTimer = window.setTimeout(() => {
      refreshSelected({ includeInspectors: false, incrementalEvents: true }).catch(() => undefined);
    }, POLL_INTERVAL_MS);
  }

  function renderTaskList() {
    const list = createElement("div", { className: "runtime-console__task-list", ariaLive: "polite" });
    if (state.listStatus === "loading") {
      list.append(createElement("p", { className: "runtime-console__muted", text: "正在读取任务..." }));
      return list;
    }
    if (!state.tasks.length) {
      list.append(createElement("p", { className: "runtime-console__muted", text: "还没有 Runtime 任务。" }));
      return list;
    }
    state.tasks.forEach((task) => {
      const taskId = taskIdOf(task);
      const selected = taskId === state.selectedTaskId;
      const button = createElement("button", {
        className: `runtime-console__task${selected ? " is-selected" : ""}`,
        type: "button",
        ariaSelected: selected,
        dataset: { action: "select-task", taskId },
      }, [
        createElement("span", { className: "runtime-console__task-title", text: compactText(taskGoalOf(task), 84) }),
        createElement("span", { className: "runtime-console__task-meta" }, [
          createElement("span", { text: taskId ? `#${taskId.slice(-8)}` : "任务 ID 未提供" }),
          createElement("span", { text: statusLabel(taskStatusOf(task)) }),
        ]),
      ]);
      list.append(button);
    });
    return list;
  }

  function renderSidebar() {
    const heading = createElement("div", { className: "runtime-console__sidebar-heading" }, [
      createElement("div", {}, [
        createElement("p", { className: "runtime-console__eyebrow", text: "ATHENA" }),
        createElement("h1", { text: "Runtime Console" }),
      ]),
      createButton("刷新", { action: "refresh-list", variant: "quiet", title: "刷新任务列表" }),
    ]);
    const navigation = createElement("nav", { className: "runtime-console__navigation", ariaLabel: "Runtime 导航" }, [
      createElement("span", { className: "is-active", text: "任务" }),
      createElement("span", { className: "is-muted", text: "Skills" }),
      createElement("span", { className: "is-muted", text: "评测" }),
    ]);
    const sidebar = createElement("aside", { className: "runtime-console__sidebar" }, [
      heading,
      navigation,
      createElement("button", {
        className: "runtime-console__skill-launcher",
        text: state.activeView === "skills" ? "返回 Runtime 任务" : "打开 Skill 评测",
        type: "button",
        dataset: { action: "select-view", view: state.activeView === "skills" ? "tasks" : "skills" },
      }),
      createElement("div", { className: "runtime-console__sidebar-section" }, [
        createElement("div", { className: "runtime-console__section-heading" }, [
          createElement("h2", { text: "任务" }),
          createElement("span", { text: String(state.tasks.length) }),
        ]),
        renderTaskList(),
      ]),
    ]);
    return sidebar;
  }

  function renderTimeline() {
    if (state.detailStatus === "loading") {
      return createEmptyState("正在加载任务", "正在同步任务、事件和检查器投影。");
    }
    if (state.detailStatus === "error") {
      return createErrorState(state.error || "任务投影读取失败", "refresh-selected");
    }
    if (!state.selectedTaskId) {
      return createEmptyState("创建或选择一个任务", "新建任务后，公开 Tick、工具调用和证据会按顺序显示在这里。");
    }
    if (!state.events.length) {
      return createEmptyState("暂无公开事件", "任务运行后，运行时会在这里写入可审计的事件。隐藏推理不会被展示。");
    }
    const list = createElement("ol", { className: "runtime-console__timeline", ariaLive: "polite" });
    state.events.forEach((event) => {
      const type = eventTypeOf(event);
      const data = eventDataOf(event);
      const entry = createElement("li", { className: "runtime-console__timeline-entry" }, [
        createElement("span", { className: "runtime-console__timeline-marker", ariaLabel: "事件" }),
        createElement("div", { className: "runtime-console__timeline-copy" }, [
          createElement("div", { className: "runtime-console__timeline-title" }, [
            createElement("strong", { text: EVENT_LABELS[type] || type.replaceAll("_", " ") }),
            createElement("time", { text: formatTime(event.created_at || event.createdAt || event.timestamp) }),
          ]),
          createElement("p", { text: eventSummary(event) }),
          Object.keys(data).length
            ? createElement("details", { className: "runtime-console__event-data" }, [
              createElement("summary", { text: "查看公开事件数据" }),
              createElement("pre", { text: publicJson(data) }),
            ])
            : null,
        ]),
      ]);
      list.append(entry);
    });
    return list;
  }

  function renderReport() {
    const report = reportOf(selectedTask());
    if (!report) return null;
    return createElement("section", { className: "runtime-console__report" }, [
      createElement("div", { className: "runtime-console__section-heading" }, [
        createElement("h2", { text: "最终报告" }),
        createElement("span", { text: "已由运行时返回" }),
      ]),
      createElement("pre", { className: "runtime-console__report-body", text: report }),
    ]);
  }

  function renderWorkspace() {
    const task = selectedTask();
    const header = createElement("header", { className: "runtime-console__workspace-header" }, [
      createElement("div", { className: "runtime-console__goal" }, [
        createElement("p", { className: "runtime-console__eyebrow", text: task ? "当前 AgentTask" : "Agent Runtime" }),
        createElement("h2", { text: task ? taskGoalOf(task) : "代码仓库诊断" }),
        task
          ? createElement("p", { className: "runtime-console__task-id", text: `Task ${taskIdOf(task)}` })
          : createElement("p", { className: "runtime-console__task-id", text: "创建一个只读代码仓库诊断任务。" }),
      ]),
      task ? createElement("div", { className: "runtime-console__workspace-actions" }, [
        (() => {
          const holder = createElement("div", { className: "runtime-console__status-holder" });
          appendStatus(holder, taskStatusOf(task));
          return holder;
        })(),
        createButton("运行", {
          action: "run-task",
          taskId: taskIdOf(task),
          variant: "primary",
          disabled: isTerminal(task) || state.busyAction !== null,
        }),
        createButton("取消", {
          action: "cancel-task",
          taskId: taskIdOf(task),
          variant: "danger",
          disabled: isTerminal(task) || state.busyAction !== null,
        }),
      ]) : null,
    ]);
    const timelineSection = createElement("section", { className: "runtime-console__timeline-panel" }, [
      createElement("div", { className: "runtime-console__section-heading" }, [
        createElement("h2", { text: "运行时间线" }),
        createElement("span", { text: `${state.events.length} 个公开事件` }),
      ]),
      renderTimeline(),
    ]);
    const workspace = createElement("main", { className: "runtime-console__workspace" }, [header, timelineSection]);
    const report = renderReport();
    if (report) workspace.append(report);
    return workspace;
  }

  function renderRunInspector(task) {
    if (!task) return createEmptyState("尚未选择任务", "选择任务后，可在这里运行、取消或补充人工输入。");
    const facts = [
      ["状态", statusLabel(taskStatusOf(task))],
      ["档位", task.profile || task.task_profile || "默认"],
      ["Tick", task.tick_count ?? task.current_tick ?? "-"],
      ["预算模式", task.budget_mode || task.budget?.mode || "-"],
      ["已消耗 Token", task.budget?.consumed_tokens ?? "-"],
      ["剩余 Token", task.budget?.remaining_tokens ?? "-"],
      ["执行后端", task.execution?.backend || "-"],
      ["决策模式", task.execution?.decision_mode || "-"],
      ["记忆策略", task.execution?.memory_strategy || "-"],
    ];
    const list = createElement("dl", { className: "runtime-console__facts" });
    facts.forEach(([label, value]) => {
      list.append(createElement("div", {}, [
        createElement("dt", { text: label }),
        createElement("dd", { text: value }),
      ]));
    });
    const controls = createElement("div", { className: "runtime-console__inspector-controls" }, [
      createButton("运行到边界", {
        action: "run-task",
        taskId: taskIdOf(task),
        variant: "primary",
        disabled: isTerminal(task) || state.busyAction !== null,
      }),
      createButton("刷新状态", { action: "refresh-selected", taskId: taskIdOf(task) }),
    ]);
    const content = createElement("div", {}, [list, controls]);
    if (["waiting", "waiting_human"].includes(taskStatusOf(task))) {
      const form = createElement("form", { className: "runtime-console__human-input", dataset: { form: "human-input" } }, [
        createElement("label", { text: "人工输入", htmlFor: "runtime-human-input" }),
        createElement("textarea", {
          id: "runtime-human-input",
          name: "input",
          placeholder: "补充 Agent 继续运行所需的信息",
        }),
        createButton("提交并继续", { variant: "primary", type: "submit", disabled: state.busyAction !== null }),
      ]);
      content.append(form);
    }
    return content;
  }

  function renderEvidenceInspector() {
    if (state.inspectorStatus === "loading") return createEmptyState("正在加载证据", "正在读取来源和 Artifact 引用。");
    if (state.inspectorStatus === "error") return createErrorState(state.error || "证据读取失败", "refresh-selected");
    if (!state.evidence.length) return createEmptyState("暂无 Evidence", "运行时确认来源后，会在这里展示可追溯的证据卡片。");
    const list = createElement("ul", { className: "runtime-console__evidence-list" });
    state.evidence.forEach((evidence) => {
      const title = evidence.claim || evidence.summary || evidence.title || "已记录 Evidence";
      const metadata = `${evidenceSourceOf(evidence)}${evidence.confidence !== undefined ? ` | 置信度 ${evidence.confidence}` : ""}`;
      list.append(createElement("li", {}, [
        createElement("strong", { text: title }),
        createElement("span", { text: metadata }),
        evidence.artifact_id || evidence.artifact?.id
          ? createElement("code", { text: `Artifact ${evidence.artifact_id || evidence.artifact.id}` })
          : null,
      ]));
    });
    return list;
  }

  function renderUsageInspector() {
    if (state.inspectorStatus === "loading") return createEmptyState("正在读取用量", "正在加载 TokenLedger 投影。");
    if (state.inspectorStatus === "error") return createErrorState(state.error || "用量读取失败", "refresh-selected");
    const entries = usageEntriesOf(state.usage);
    if (!state.usage && !entries.length) return createEmptyState("暂无用量记录", "模型调用完成后，这里会显示实际 Token、模型路由原因与预算状态。");
    const total = usageTotalOf(state.usage, entries);
    const summary = createElement("div", { className: "runtime-console__usage-summary" }, [
      createElement("strong", { text: formatNumber(total) }),
      createElement("span", { text: "Token" }),
    ]);
    const list = createElement("ul", { className: "runtime-console__usage-list" });
    entries.forEach((entry) => {
      list.append(createElement("li", {}, [
        createElement("strong", { text: entry.purpose || entry.decision_purpose || "模型调用" }),
        createElement("span", { text: `${entry.model || entry.model_tier || "模型未提供"} | ${formatNumber(entry.total_tokens ?? entry.tokens)} Token` }),
        createElement("small", { text: entry.route_reason || entry.reason_code || "路由原因未提供" }),
      ]));
    });
    return createElement("div", {}, [summary, list]);
  }

  function renderInspectorContent() {
    const task = selectedTask();
    if (state.activeInspector === "context") {
      if (state.inspectorStatus === "loading") return createEmptyState("正在加载上下文", "只显示当前 ContextSnapshot 的公开投影。");
      if (state.inspectorStatus === "error") return createErrorState(state.error || "上下文读取失败", "refresh-selected");
      return createJsonBlock(state.context, "运行时编译上下文后，将在这里展示公开投影。隐藏推理不会被包含。");
    }
    if (state.activeInspector === "evidence") return renderEvidenceInspector();
    if (state.activeInspector === "usage") return renderUsageInspector();
    return renderRunInspector(task);
  }

  function renderInspector() {
    const tabs = [
      ["run", "运行"],
      ["context", "上下文"],
      ["evidence", "证据"],
      ["usage", "用量"],
    ];
    const tabList = createElement("div", { className: "runtime-console__tabs", role: "tablist", ariaLabel: "任务检查器" });
    tabs.forEach(([key, label]) => {
      tabList.append(createElement("button", {
        className: `runtime-console__tab${state.activeInspector === key ? " is-active" : ""}`,
        text: label,
        type: "button",
        role: "tab",
        ariaSelected: state.activeInspector === key,
        dataset: { action: "select-inspector", inspector: key },
      }));
    });
    return createElement("aside", { className: "runtime-console__inspector" }, [
      tabList,
      createElement("div", { className: "runtime-console__inspector-body", role: "tabpanel" }, renderInspectorContent()),
    ]);
  }

  function renderComposer() {
    const profile = createElement("select", { name: "profile", ariaLabel: "任务档位" }, [
      createElement("option", { value: "STANDARD", text: "STANDARD" }),
      createElement("option", { value: "SIMPLE", text: "SIMPLE" }),
      createElement("option", { value: "COMPLEX", text: "COMPLEX" }),
    ]);
    return createElement("form", { className: "runtime-console__composer", dataset: { form: "create-task" } }, [
      createElement("label", { className: "runtime-console__composer-field", text: "任务目标" }, [
        createElement("textarea", {
          name: "goal",
          placeholder: "描述需要诊断的代码问题",
          ariaLabel: "任务目标",
        }),
      ]),
      createElement("label", { className: "runtime-console__repository-field", text: "仓库路径" }, [
        createElement("input", {
          name: "repository_path",
          placeholder: "D:\\workspace\\repository",
          ariaLabel: "仓库路径",
        }),
      ]),
      profile,
      createButton("创建任务", { variant: "primary", type: "submit", disabled: state.busyAction !== null }),
    ]);
  }

  function renderSkillView() {
    const host = createElement("div", { className: "runtime-console__skill-host" });
    return {
      shell: createElement("section", { className: "runtime-console", ariaLive: "polite" }, [host]),
      host,
    };
  }

  function render() {
    if (state.destroyed) return;
    if (state.activeView === "skills") {
      state.skillController?.destroy();
      const view = renderSkillView();
      root.replaceChildren(view.shell);
      state.skillController = mountSkillEvaluation(view.host, {
        api,
        initialSection: options.initialSkillSection,
      });
      return;
    }
    state.skillController?.destroy();
    state.skillController = null;
    const shell = createElement("section", { className: "runtime-console", ariaLive: "polite" });
    if (state.error && state.listStatus === "error") {
      shell.append(createErrorState(state.error, "refresh-list"));
    }
    if (state.error && state.listStatus !== "error" && state.detailStatus !== "error" && state.inspectorStatus !== "error") {
      shell.append(createErrorState(state.error, "refresh-selected"));
    }
    const grid = createElement("div", { className: "runtime-console__grid" }, [
      renderSidebar(),
      renderWorkspace(),
      renderInspector(),
    ]);
    shell.append(grid, renderComposer());
    root.replaceChildren(shell);
  }

  function updateTaskInList(task) {
    const id = taskIdOf(task);
    if (!id) return;
    const index = state.tasks.findIndex((item) => taskIdOf(item) === id);
    if (index >= 0) state.tasks[index] = { ...state.tasks[index], ...task };
    else state.tasks.unshift(task);
  }

  async function loadTaskList({ selectFirst = true } = {}) {
    state.listStatus = "loading";
    render();
    try {
      const payload = await api.get(RUNTIME_ROOT);
      if (state.destroyed) return [];
      state.tasks = asItems(payload).sort((left, right) => {
        const leftDate = new Date(left.updated_at || left.created_at || 0).valueOf();
        const rightDate = new Date(right.updated_at || right.created_at || 0).valueOf();
        return rightDate - leftDate;
      });
      state.listStatus = "ready";
      state.error = null;
      if (state.selectedTaskId && !state.tasks.some((task) => taskIdOf(task) === state.selectedTaskId)) {
        state.selectedTaskId = null;
        state.task = null;
      }
      if (!state.selectedTaskId && selectFirst && state.tasks[0]) {
        await selectTask(taskIdOf(state.tasks[0]));
        return state.tasks;
      }
      render();
      return state.tasks;
    } catch (error) {
      if (!state.destroyed) {
        state.listStatus = "error";
        state.error = error.message || "任务列表读取失败";
        render();
      }
      throw error;
    }
  }

  function mergeEvents(events) {
    const known = new Set(state.events.map(eventIdOf).filter(Boolean));
    events.forEach((event) => {
      const id = eventIdOf(event);
      if (!id || !known.has(id)) {
        state.events.push(event);
        if (id) known.add(id);
      }
    });
    state.events.sort((left, right) => {
      const leftOrder = Number(left.sequence ?? left.seq ?? 0);
      const rightOrder = Number(right.sequence ?? right.seq ?? 0);
      return leftOrder - rightOrder;
    });
  }

  async function loadEvents(taskId, version, { incremental = false } = {}) {
    const cursor = incremental && state.eventCursor ? `?after=${encodeURIComponent(state.eventCursor)}` : "";
    const payload = await api.get(`${RUNTIME_ROOT}/${encodeURIComponent(taskId)}/events${cursor}`);
    if (state.destroyed || version !== state.requestVersion || taskId !== state.selectedTaskId) return;
    const events = asItems(payload, ["items", "events"]);
    if (incremental) mergeEvents(events);
    else state.events = events;
    state.eventCursor = eventCursorOf(payload, state.events, state.eventCursor);
  }

  async function loadInspectors(taskId, version) {
    const results = await Promise.allSettled([
      api.get(`${RUNTIME_ROOT}/${encodeURIComponent(taskId)}/evidence`),
      api.get(`${RUNTIME_ROOT}/${encodeURIComponent(taskId)}/context`),
      api.get(`${RUNTIME_ROOT}/${encodeURIComponent(taskId)}/usage`),
    ]);
    if (state.destroyed || version !== state.requestVersion || taskId !== state.selectedTaskId) return;
    const [evidence, context, usage] = results;
    if (evidence.status === "fulfilled") state.evidence = asItems(evidence.value, ["items", "evidence"]);
    if (context.status === "fulfilled") state.context = context.value;
    if (usage.status === "fulfilled") state.usage = usage.value;
    const rejection = results.find((result) => result.status === "rejected");
    state.inspectorStatus = rejection ? "error" : "ready";
    if (rejection) state.error = rejection.reason?.message || "检查器投影读取失败";
  }

  async function selectTask(taskId) {
    if (!taskId || state.destroyed) return;
    window.clearTimeout(state.pollTimer);
    state.selectedTaskId = taskId;
    state.task = state.tasks.find((task) => taskIdOf(task) === taskId) || null;
    state.events = [];
    state.eventCursor = null;
    state.evidence = [];
    state.context = null;
    state.usage = null;
    state.detailStatus = "loading";
    state.inspectorStatus = "loading";
    const version = ++state.requestVersion;
    render();
    try {
      const [taskResult, eventResult, inspectorResult] = await Promise.allSettled([
        api.get(`${RUNTIME_ROOT}/${encodeURIComponent(taskId)}`),
        loadEvents(taskId, version),
        loadInspectors(taskId, version),
      ]);
      if (state.destroyed || version !== state.requestVersion) return;
      if (taskResult.status === "rejected") throw taskResult.reason;
      state.task = taskProjection(taskResult.value);
      updateTaskInList(state.task);
      if (eventResult.status === "rejected") throw eventResult.reason;
      state.detailStatus = "ready";
      if (inspectorResult.status === "rejected") {
        state.inspectorStatus = "error";
      }
      state.error = null;
      render();
      setPolling();
    } catch (error) {
      if (!state.destroyed && version === state.requestVersion) {
        state.detailStatus = "error";
        state.inspectorStatus = "error";
        state.error = error.message || "任务详情读取失败";
        render();
      }
    }
  }

  async function refreshSelected({ includeInspectors = true, incrementalEvents = true } = {}) {
    const taskId = state.selectedTaskId;
    if (!taskId || state.destroyed) return;
    const version = ++state.requestVersion;
    try {
      const requests = [
        api.get(`${RUNTIME_ROOT}/${encodeURIComponent(taskId)}`),
        loadEvents(taskId, version, { incremental: incrementalEvents }),
      ];
      if (includeInspectors) requests.push(loadInspectors(taskId, version));
      const [taskPayload] = await Promise.all(requests);
      if (state.destroyed || version !== state.requestVersion) return;
      const task = taskProjection(taskPayload);
      state.task = task;
      updateTaskInList(task);
      state.detailStatus = "ready";
      state.error = null;
      render();
      setPolling();
    } catch (error) {
      if (!state.destroyed && version === state.requestVersion) {
        state.error = error.message || "刷新任务失败";
        state.detailStatus = "error";
        render();
      }
    }
  }

  async function createTask(form) {
    const formData = new FormData(form);
    const goal = String(formData.get("goal") || "").trim();
    const repositoryPath = String(formData.get("repository_path") || "").trim();
    const profile = String(formData.get("profile") || "STANDARD");
    if (!goal || !repositoryPath) {
      state.error = "请填写任务目标和仓库路径。";
      render();
      return;
    }
    state.busyAction = "create";
    state.error = null;
    render();
    try {
      const created = await api.post(RUNTIME_ROOT, {
        goal,
        repository_path: repositoryPath,
        profile,
      });
      const task = taskProjection(created);
      if (state.destroyed) return;
      form.reset();
      updateTaskInList(task);
      state.listStatus = "ready";
      await selectTask(taskIdOf(task));
    } catch (error) {
      if (!state.destroyed) {
        state.error = error.message || "创建任务失败";
        render();
      }
    } finally {
      if (!state.destroyed) {
        state.busyAction = null;
        render();
      }
    }
  }

  async function runTask(taskId) {
    if (!taskId || state.busyAction || state.destroyed) return;
    state.busyAction = "run";
    state.error = null;
    render();
    try {
      const response = await api.post(`${RUNTIME_ROOT}/${encodeURIComponent(taskId)}/run`, {});
      const task = taskProjection(response);
      updateTaskInList(task);
      state.task = task;
      await refreshSelected({ includeInspectors: true, incrementalEvents: true });
    } catch (error) {
      if (!state.destroyed) {
        state.error = error.message || "运行任务失败";
        render();
      }
    } finally {
      if (!state.destroyed) {
        state.busyAction = null;
        render();
      }
    }
  }

  async function cancelTask(taskId) {
    if (!taskId || state.busyAction || state.destroyed) return;
    state.busyAction = "cancel";
    state.error = null;
    render();
    try {
      const response = await api.post(`${RUNTIME_ROOT}/${encodeURIComponent(taskId)}/cancel`, {});
      const task = taskProjection(response);
      updateTaskInList(task);
      state.task = task;
      await refreshSelected({ includeInspectors: true, incrementalEvents: true });
    } catch (error) {
      if (!state.destroyed) {
        state.error = error.message || "取消任务失败";
        render();
      }
    } finally {
      if (!state.destroyed) {
        state.busyAction = null;
        render();
      }
    }
  }

  async function submitHumanInput(form) {
    if (!state.selectedTaskId || state.busyAction || state.destroyed) return;
    const input = String(new FormData(form).get("input") || "").trim();
    if (!input) {
      state.error = "请输入需要补充给 Agent 的信息。";
      render();
      return;
    }
    state.busyAction = "human-input";
    state.error = null;
    render();
    try {
      const response = await api.post(currentTaskPath("/input"), { input });
      const task = taskProjection(response);
      updateTaskInList(task);
      state.task = task;
      await refreshSelected({ includeInspectors: true, incrementalEvents: true });
    } catch (error) {
      if (!state.destroyed) {
        state.error = error.message || "提交人工输入失败";
        render();
      }
    } finally {
      if (!state.destroyed) {
        state.busyAction = null;
        render();
      }
    }
  }

  async function handleClick(event) {
    const target = event.target.closest("[data-action]");
    if (!target || !root.contains(target)) return;
    const { action, taskId, inspector, view } = target.dataset;
    if (action === "select-view") {
      state.activeView = view === "skills" ? "skills" : "tasks";
      render();
      if (state.activeView === "tasks" && !state.tasks.length) await loadTaskList({ selectFirst: false });
      return;
    }
    if (action === "select-task") await selectTask(taskId);
    if (action === "refresh-list") await loadTaskList({ selectFirst: false });
    if (action === "refresh-selected") await refreshSelected();
    if (action === "run-task") await runTask(taskId || state.selectedTaskId);
    if (action === "cancel-task") await cancelTask(taskId || state.selectedTaskId);
    if (action === "select-inspector") {
      state.activeInspector = inspector;
      render();
    }
  }

  async function handleSubmit(event) {
    const form = event.target;
    const name = form?.dataset?.form;
    if (!name || !root.contains(form)) return;
    event.preventDefault();
    if (name === "create-task") await createTask(form);
    if (name === "human-input") await submitHumanInput(form);
  }

  root.addEventListener("click", handleClick);
  root.addEventListener("submit", handleSubmit);
  loadTaskList().catch(() => undefined);

  return Object.freeze({
    refresh: () => loadTaskList({ selectFirst: false }),
    destroy: () => {
      state.destroyed = true;
      window.clearTimeout(state.pollTimer);
      state.skillController?.destroy();
      state.skillController = null;
      root.removeEventListener("click", handleClick);
      root.removeEventListener("submit", handleSubmit);
      root.replaceChildren();
    },
  });
}
