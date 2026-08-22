const CANDIDATE_ROOT = "/api/skill-candidates";
const LEGACY_CANDIDATE_ROOT = "/api/runtime/skills";
const EVALUATION_ROOT = "/api/skill-evaluation";
const CANDIDATE_EVALUATION_ROOT = "/api/skill-evaluation/candidates/";
const RELEASE_ROOT = "/api/skill-release";
const LEGACY_REVIEW_PATH = "/review";
const BACKEND_UNAVAILABLE_MARKER = "backend-unavailable";

const METRICS = [
  ["task_success", "任务成功"],
  ["evidence_retention", "Evidence 保留"],
  ["tick_count", "Tick"],
  ["tool_call_count", "工具调用"],
  ["input_tokens", "输入 Token"],
  ["total_tokens", "总 Token"],
  ["latency_ms", "延迟 ms"],
  ["safety_violations", "安全违规"],
  ["failure_reason", "失败原因"],
];

const SECTIONS = Object.freeze([
  ["candidate", "Candidate"],
  ["replay", "Replay A/B"],
  ["shadow", "Shadow"],
  ["review", "Review"],
  ["release", "Release"],
  ["rollback", "Rollback"],
]);

function asRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function firstRecord(value) {
  const record = asRecord(value);
  return asRecord(record.candidate).id ? record.candidate : record;
}

function listPayload(value) {
  const record = asRecord(value);
  return asArray(record.items || record.candidates || record.data);
}

function candidateId(candidate) {
  return String(candidate?.id || candidate?.candidate_id || "");
}

function candidateName(candidate) {
  return String(candidate?.name || "未命名 Candidate");
}

function candidateStatus(candidate) {
  return String(candidate?.status || candidate?.evaluation_status || "unknown").toLowerCase();
}

function finite(value) {
  return Number.isFinite(Number(value));
}

function formatMetric(key, value) {
  if (value === null || value === undefined || value === "") return "-";
  if (key === "task_success") return value === true ? "成功" : "失败";
  if (key === "failure_reason") return String(value);
  if (key === "evidence_retention") {
    return finite(value) ? `${(Number(value) * 100).toFixed(1)}%` : "-";
  }
  if (finite(value)) return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(Number(value));
  return String(value);
}

function formatStatus(value) {
  const normalized = String(value || "unknown").toLowerCase();
  const labels = {
    candidate: "Candidate",
    validation_passed: "静态校验通过",
    replay_ab_passed: "Replay A/B 通过",
    rejected: "Rejected",
    evaluation_failed: "评测失败",
    review_pending: "等待 Review",
    passed: "Passed",
    failed: "Failed",
    active: "Active",
    archived: "Archived",
    unknown: "未知",
  };
  return labels[normalized] || normalized.replaceAll("_", " ");
}

function statusTone(value) {
  const normalized = String(value || "unknown").toLowerCase();
  if (["passed", "replay_ab_passed", "validation_passed", "active"].includes(normalized)) return "success";
  if (["rejected", "evaluation_failed", "failed"].includes(normalized)) return "danger";
  if (["review_pending", "candidate"].includes(normalized)) return "progress";
  return "neutral";
}

function errorCode(error) {
  return typeof error?.code === "string" ? error.code : "REQUEST_FAILED";
}

function endpointId(value) {
  return encodeURIComponent(String(value || ""));
}

function createElement(tag, {
  className,
  text,
  type,
  disabled,
  title,
  ariaLabel,
  role,
  name,
  placeholder,
  required,
  dataset,
} = {}, children = []) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (type) element.type = type;
  if (disabled) element.disabled = true;
  if (title) element.title = title;
  if (ariaLabel) element.setAttribute("aria-label", ariaLabel);
  if (role) element.setAttribute("role", role);
  if (name) element.name = name;
  if (placeholder) element.placeholder = placeholder;
  if (required) element.required = true;
  Object.entries(dataset || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null) element.dataset[key] = String(value);
  });
  for (const child of asArray(children).concat(children && !Array.isArray(children) ? [children] : [])) {
    if (child) element.append(child);
  }
  return element;
}

function button(label, action, { candidateId: id, section, variant = "secondary", disabled = false, title } = {}) {
  return createElement("button", {
    className: `runtime-console__button runtime-console__skill-button runtime-console__button--${variant}`,
    text: label,
    type: "button",
    disabled,
    title,
    dataset: { action, candidateId: id, section },
  });
}

function statusBadge(value) {
  return createElement("span", {
    className: `runtime-console__status runtime-console__status--${statusTone(value)}`,
    text: formatStatus(value),
  });
}

function empty(title, detail) {
  return createElement("section", { className: "runtime-console__skill-empty" }, [
    createElement("h3", { text: title }),
    createElement("p", { text: detail }),
  ]);
}

function errorState(code, retryAction) {
  return createElement("section", { className: "runtime-console__skill-error", role: "alert" }, [
    createElement("strong", { text: code }),
    createElement("p", { text: "后端投影不可用，页面没有生成替代指标。" }),
    retryAction ? button("重试", retryAction, { variant: "secondary" }) : null,
  ]);
}

function valueList(values, emptyText = "暂无") {
  const list = createElement("ul", { className: "runtime-console__skill-inline-list" });
  const items = asArray(values).filter((value) => value !== null && value !== undefined && String(value).trim());
  if (!items.length) {
    list.append(createElement("li", { text: emptyText }));
    return list;
  }
  items.forEach((value) => list.append(createElement("li", { text: value })));
  return list;
}

function factList(entries) {
  const list = createElement("dl", { className: "runtime-console__skill-facts" });
  entries.forEach(([label, value]) => {
    list.append(createElement("div", {}, [
      createElement("dt", { text: label }),
      createElement("dd", { text: value === undefined || value === null || value === "" ? "-" : value }),
    ]));
  });
  return list;
}

function reportFor(state, kind) {
  return kind === "replay" ? state.replay : state.shadow;
}

function failedGates(report) {
  const checks = asRecord(asRecord(report).gate).checks || asRecord(report).gate_checks;
  return Object.entries(asRecord(checks)).filter(([, passed]) => passed !== true).map(([key]) => key);
}

function renderGate(report) {
  if (!report) return empty("暂无门禁结果", "运行对应的评测后，页面会读取后端返回的门禁检查。");
  const failures = failedGates(report);
  const reason = report.failure_reason || "";
  const content = [];
  content.push(factList([
    ["运行状态", formatStatus(report.status)],
    ["Case 数", report.case_count],
    ["测量来源", report.measurement],
    ["失败原因", reason || "-"],
  ]));
  if (failures.length || reason) {
    content.push(createElement("div", { className: "runtime-console__skill-gate-failures" }, [
      createElement("strong", { text: "未通过门禁" }),
      valueList([...failures, ...(reason ? [reason] : [])]),
    ]));
  } else {
    content.push(createElement("p", { className: "runtime-console__skill-safe-note", text: "后端门禁全部通过。该结果仍不会自动发布。" }));
  }
  return createElement("div", {}, content);
}

function renderMetricsTable(comparisons, leftKey, rightKey) {
  if (!comparisons.length) return empty("暂无逐 Case 指标", "后端尚未返回逐 Case 运行结果。");
  const table = createElement("table", { className: "runtime-console__skill-metrics" });
  const head = createElement("thead", {}, [
    createElement("tr", {}, [
      createElement("th", { text: "Case" }),
      createElement("th", { text: "组别" }),
      ...METRICS.map(([, label]) => createElement("th", { text: label })),
    ]),
  ]);
  const body = createElement("tbody");
  comparisons.forEach((comparison) => {
    [leftKey, rightKey].forEach((group) => {
      const metrics = asRecord(comparison[group]);
      body.append(createElement("tr", {}, [
        createElement("th", { text: comparison.case_id || "-" }),
        createElement("td", { text: group }),
        ...METRICS.map(([key]) => createElement("td", { text: formatMetric(key, metrics[key]) })),
      ]));
    });
  });
  table.append(head, body);
  return createElement("div", { className: "runtime-console__skill-table-wrap" }, [table]);
}

function renderAggregate(report) {
  const aggregate = asRecord(report?.aggregate);
  const sections = Object.entries(aggregate);
  if (!sections.length) return empty("暂无聚合指标", "后端没有返回聚合结果。");
  const list = createElement("div", { className: "runtime-console__skill-aggregate" });
  sections.forEach(([name, metrics]) => {
    const entries = Object.entries(asRecord(metrics)).map(([key, value]) => [key, formatMetric(key, value)]);
    list.append(createElement("section", {}, [
      createElement("h4", { text: name }),
      factList(entries),
    ]));
  });
  return list;
}

function renderReportPanel(state, kind) {
  const report = reportFor(state, kind);
  const comparisons = asArray(report?.comparisons);
  const groupNames = kind === "replay" ? ["baseline", "candidate"] : ["main", "shadow"];
  return createElement("div", { className: "runtime-console__skill-report" }, [
    createElement("div", { className: "runtime-console__skill-actions" }, [
      button(
        kind === "replay" ? "运行 Replay A/B" : "运行 Shadow",
        kind === "replay" ? "run-replay" : "run-shadow",
        {
          candidateId: candidateId(state.candidate),
          variant: "primary",
          disabled: Boolean(state.busyAction) || !state.candidate,
        },
      ),
    ]),
    renderGate(report),
    createElement("section", { className: "runtime-console__skill-section-block" }, [
      createElement("h3", { text: "逐 Case 指标" }),
      renderMetricsTable(comparisons, ...groupNames),
    ]),
    createElement("section", { className: "runtime-console__skill-section-block" }, [
      createElement("h3", { text: "聚合对比" }),
      renderAggregate(report),
    ]),
  ]);
}

function renderCandidateSection(state) {
  const candidate = state.candidate;
  if (!candidate) return empty("选择一个 Candidate", "候选 Skill 的所有事实都从租户隔离 API 读取。");
  const activationAllowed = candidate.activation_allowed === true || candidate.online_eligible === true;
  return createElement("div", { className: "runtime-console__skill-content" }, [
    createElement("section", { className: "runtime-console__skill-card" }, [
      createElement("div", { className: "runtime-console__skill-card-heading" }, [
        createElement("div", {}, [
          createElement("p", { className: "runtime-console__eyebrow", text: "Candidate" }),
          createElement("h3", { text: candidateName(candidate) }),
          createElement("code", { text: candidateId(candidate) }),
        ]),
        statusBadge(candidateStatus(candidate)),
      ]),
      factList([
        ["评测状态", formatStatus(candidate.evaluation_status)],
        ["环境", candidate.environment_type],
        ["风险等级", candidate.risk_level],
        ["版本", candidate.version],
        ["激活许可", activationAllowed ? "允许" : "禁止"],
        ["Replay 报告", candidate.replay_report_id || "尚未生成"],
        ["Shadow 报告", candidate.shadow_report_id || "尚未生成"],
      ]),
    ]),
    createElement("section", { className: "runtime-console__skill-card" }, [
      createElement("h3", { text: "只读工具范围" }),
      valueList(candidate.allowed_tools || candidate.capabilities),
      createElement("h3", { text: "触发条件" }),
      createElement("pre", { className: "runtime-console__skill-code", text: JSON.stringify(candidate.trigger || {}, null, 2) }),
    ]),
    createElement("section", { className: "runtime-console__skill-card" }, [
      createElement("h3", { text: "当前门禁失败原因" }),
      state.replay || state.shadow
        ? valueList([
          ...failedGates(state.replay),
          ...failedGates(state.shadow),
          state.replay?.failure_reason,
          state.shadow?.failure_reason,
        ].filter(Boolean), "没有读取到失败原因")
        : createElement("p", { className: "runtime-console__skill-muted", text: "尚未读取到 Replay 或 Shadow 报告。" }),
    ]),
  ]);
}

function renderReviewSection(state) {
  const candidate = state.candidate;
  if (!candidate) return empty("选择 Candidate 后 Review", "Review 请求会提交到后端生命周期 API。");
  const form = createElement("form", { className: "runtime-console__skill-review-form", dataset: { form: "review" } }, [
    createElement("label", { text: "Reviewer" }, [
      createElement("input", { name: "reviewer", required: true, placeholder: "输入审查人标识" }),
    ]),
    createElement("label", { text: "审查备注" }, [
      createElement("textarea", { name: "note", required: true, placeholder: "记录基于门禁和逐 Case 指标的判断" }),
    ]),
    createElement("div", { className: "runtime-console__skill-actions" }, [
      button("批准 Review", "review-approve", { candidateId: candidateId(candidate), variant: "primary", disabled: Boolean(state.busyAction) }),
      button("拒绝 Review", "review-reject", { candidateId: candidateId(candidate), variant: "danger", disabled: Boolean(state.busyAction) }),
    ]),
  ]);
  return createElement("div", { className: "runtime-console__skill-content" }, [
    createElement("section", { className: "runtime-console__skill-card" }, [
      createElement("h3", { text: "Review 状态" }),
      factList([
        ["Candidate 状态", formatStatus(candidateStatus(candidate))],
        ["审查人", candidate.reviewed_by],
        ["审查结论", candidate.review_approved === true ? "批准" : candidate.review_approved === false ? "拒绝" : "未审查"],
        ["审查备注", candidate.review_note],
      ]),
    ]),
    createElement("section", { className: "runtime-console__skill-card" }, [form]),
  ]);
}

function renderReleaseSection(state, { onHandoff }) {
  const candidate = state.candidate;
  if (!candidate) return empty("选择 Candidate 后查看发布入口", "发布必须以真实的后端生命周期 API 为准。");
  const allowed = candidate.activation_allowed === true
    || candidate.online_eligible === true
    || (candidate.evaluation_status === "replay_ab_passed" && Boolean(candidate.shadow_report_id));
  return createElement("div", { className: "runtime-console__skill-content" }, [
    createElement("section", { className: "runtime-console__skill-card" }, [
      createElement("h3", { text: "Release" }),
      createElement("p", { className: "runtime-console__skill-muted", text: allowed ? "后端投影标记为可激活，请先完成人工确认。" : "当前 Candidate 的激活许可为禁止，不能伪造发布成功。" }),
      createElement("div", { className: "runtime-console__skill-actions" }, [
        button("发布交接", "handoff", { candidateId: candidateId(candidate), variant: "primary", disabled: Boolean(state.busyAction) || !allowed, title: allowed ? "读取后端交接投影" : "activation_allowed=false" }),
        button("进入人工审批并发布", "select-section", { candidateId: candidateId(candidate), section: "review", variant: "primary", disabled: Boolean(state.busyAction) || !allowed, title: allowed ? "提交人工审批并调用真实 Release API" : "Replay/Shadow 门禁尚未通过" }),
      ]),
      state.handoff ? createElement("pre", { className: "runtime-console__skill-code", text: JSON.stringify(state.handoff, null, 2) }) : null,
      state.handoff && onHandoff ? createElement("p", { className: "runtime-console__skill-safe-note", text: "已读取交接投影；未执行发布。" }) : null,
    ]),
  ]);
}

function renderRollbackSection(state) {
  const candidate = state.candidate;
  if (!candidate) return empty("选择 Candidate 后查看回滚入口", "回滚目标和版本必须由后端 Skill Repository 提供。");
  return createElement("div", { className: "runtime-console__skill-content" }, [
    createElement("section", { className: "runtime-console__skill-card" }, [
      createElement("h3", { text: "Rollback" }),
      createElement("p", { className: "runtime-console__skill-muted" }, "当前后端没有 Candidate Release/Rollback API，页面不会发送猜测性的写请求。"),
      createElement("div", { className: "runtime-console__skill-actions" }, [
        button("填写回滚参数", "rollback-form", { candidateId: candidateId(candidate), disabled: false, title: "通过真实 Skill Release API 回滚" }),
      ]),
      createElement("p", { className: "runtime-console__skill-boundary", text: "回滚必须提供同租户 Skill、目标版本和人工 reviewer。" }),
    ]),
  ]);
}

function renderReleaseSectionV2(state, { onHandoff }) {
  const candidate = state.candidate;
  if (!candidate) return empty("选择 Candidate 后查看发布结果", "发布必须以真实后端生命周期 API 为准。");
  const ready = candidate.activation_allowed === true
    || candidate.online_eligible === true
    || (candidate.evaluation_status === "replay_ab_passed" && Boolean(candidate.shadow_report_id));
  return createElement("div", { className: "runtime-console__skill-content" }, [
    createElement("section", { className: "runtime-console__skill-card" }, [
      createElement("h3", { text: "Release" }),
      createElement("p", { className: "runtime-console__skill-muted", text: state.release ? "已由后端完成人工审核并激活 Skill Version。" : ready ? "请在 Review 页面填写人工 reviewer 后批准发布。" : "Replay/Shadow 门禁尚未通过，发布入口保持关闭。" }),
      state.release ? createElement("pre", { className: "runtime-console__skill-code", text: JSON.stringify(state.release, null, 2) }) : null,
      !state.release && ready && onHandoff ? button("查看只读交接投影", "handoff", { candidateId: candidateId(candidate), variant: "secondary", disabled: Boolean(state.busyAction) }) : null,
      state.handoff ? createElement("pre", { className: "runtime-console__skill-code", text: JSON.stringify(state.handoff, null, 2) }) : null,
    ]),
  ]);
}

function renderRollbackSectionV2(state) {
  const candidate = state.candidate;
  if (!candidate) return empty("选择 Candidate 后查看回滚入口", "回滚目标由后端 Skill Repository 提供。");
  const releaseVersion = state.release?.version || {};
  const form = createElement("form", { className: "runtime-console__skill-review-form", dataset: { form: "rollback" } }, [
    createElement("label", { text: "Skill ID" }, [
      createElement("input", { name: "skill_id", required: true, value: releaseVersion.skill_id || candidate.skill_id || "", placeholder: "skill-..." }),
    ]),
    createElement("label", { text: "目标版本 ID" }, [
      createElement("input", { name: "target_version_id", required: true, placeholder: "skill-version-..." }),
    ]),
    createElement("label", { text: "人工 reviewer" }, [
      createElement("input", { name: "reviewed_by", required: true, placeholder: "输入审查人标识" }),
    ]),
    createElement("label", { text: "回滚原因" }, [
      createElement("textarea", { name: "note", required: true, placeholder: "记录线上回归或安全原因" }),
    ]),
    createElement("div", { className: "runtime-console__skill-actions" }, [
      button("执行回滚", "rollback", { candidateId: candidateId(candidate), variant: "danger", disabled: Boolean(state.busyAction) }),
    ]),
  ]);
  return createElement("div", { className: "runtime-console__skill-content" }, [
    createElement("section", { className: "runtime-console__skill-card" }, [
      createElement("h3", { text: "Rollback" }),
      createElement("p", { className: "runtime-console__skill-muted", text: "回滚由后端原子恢复旧 Active 版本，并要求人工 reviewer。" }),
      form,
      state.rollback ? createElement("pre", { className: "runtime-console__skill-code", text: JSON.stringify(state.rollback, null, 2) }) : null,
    ]),
  ]);
}

function renderSidebar(state, refresh) {
  const list = createElement("div", { className: "runtime-console__skill-candidates" });
  if (state.listStatus === "loading") list.append(createElement("p", { className: "runtime-console__skill-muted", text: "正在读取 Candidate..." }));
  else if (state.listStatus === "error") list.append(errorState(state.errorCode, refresh));
  else if (!state.candidates.length) list.append(empty("暂无 Candidate", "后端尚未返回候选 Skill。"));
  else state.candidates.forEach((candidate) => {
    const selected = candidateId(candidate) === candidateId(state.candidate);
    list.append(createElement("button", {
      className: `runtime-console__skill-candidate${selected ? " is-selected" : ""}`,
      type: "button",
      ariaLabel: `选择 ${candidateName(candidate)}`,
      dataset: { action: "select-candidate", candidateId: candidateId(candidate) },
    }, [
      createElement("strong", { text: candidateName(candidate) }),
      createElement("span", { text: `${candidateId(candidate).slice(-10)} · ${formatStatus(candidateStatus(candidate))}` }),
    ]));
  });
  return createElement("aside", { className: "runtime-console__skill-sidebar" }, [
    createElement("div", { className: "runtime-console__skill-sidebar-heading" }, [
      createElement("div", {}, [
        createElement("p", { className: "runtime-console__eyebrow", text: "SKILL LIFECYCLE" }),
        createElement("h2", { text: "Candidate 管理" }),
      ]),
      button("刷新", "refresh-skills", { variant: "quiet" }),
    ]),
    createElement("div", { className: "runtime-console__skill-sidebar-list" }, [
      createElement("div", { className: "runtime-console__section-heading" }, [
        createElement("h3", { text: "候选列表" }),
        createElement("span", { text: String(state.candidates.length) }),
      ]),
      list,
    ]),
  ]);
}

function renderPage(state, handlers) {
  const section = state.section;
  const candidate = state.candidate;
  let content;
  if (state.detailStatus === "loading") content = empty("正在读取 Candidate", "页面只展示后端返回的租户隔离事实。");
  else if (state.detailStatus === "error") content = errorState(state.errorCode, handlers.refreshSelected);
  else if (section === "candidate") content = renderCandidateSection(state);
  else if (section === "replay") content = renderReportPanel(state, "replay");
  else if (section === "shadow") content = renderReportPanel(state, "shadow");
  else if (section === "review") content = renderReviewSection(state);
  else if (section === "release") content = renderReleaseSectionV2(state, { onHandoff: handlers.handoff });
  else content = renderRollbackSectionV2(state);

  const tabs = createElement("nav", { className: "runtime-console__skill-tabs", role: "tablist" });
  SECTIONS.forEach(([key, label]) => tabs.append(createElement("button", {
    className: `runtime-console__skill-tab${section === key ? " is-active" : ""}`,
    text: label,
    type: "button",
    role: "tab",
    dataset: { action: "select-section", section: key },
  })));
  const header = createElement("header", { className: "runtime-console__skill-header" }, [
    createElement("div", {}, [
      createElement("p", { className: "runtime-console__eyebrow", text: "Agent Runtime" }),
      createElement("h1", { text: candidate ? candidateName(candidate) : "Skill Evaluation" }),
      createElement("p", { className: "runtime-console__skill-muted", text: candidate ? candidateId(candidate) : "Candidate / Replay / Shadow / Review" }),
    ]),
    createElement("div", { className: "runtime-console__skill-actions" }, [
      candidate ? statusBadge(candidateStatus(candidate)) : null,
      createElement("button", {
        className: "runtime-console__button runtime-console__button--quiet",
        text: "返回 Runtime 任务",
        type: "button",
        dataset: { action: "select-view", view: "tasks" },
      }),
    ]),
  ]);
  const main = createElement("main", { className: "runtime-console__skill-main" }, [header, tabs, content]);
  return createElement("section", { className: "runtime-console__skill-layout" }, [
    renderSidebar(state, handlers.refresh),
    main,
  ]);
}

export function mountSkillEvaluation(host, options = {}) {
  if (!host || typeof host.replaceChildren !== "function") throw new TypeError("Skill evaluation requires a DOM root");
  const api = options.api;
  if (!api || typeof api.get !== "function" || typeof api.post !== "function") throw new TypeError("Skill evaluation requires an API client");
  const state = {
    candidates: [],
    candidate: null,
    replay: null,
    shadow: null,
    handoff: null,
    release: null,
    rollback: null,
    section: SECTIONS.some(([key]) => key === options.initialSection) ? options.initialSection : "candidate",
    listStatus: "loading",
    detailStatus: "idle",
    errorCode: null,
    busyAction: null,
    destroyed: false,
  };

  function render() {
    if (!state.destroyed) host.replaceChildren(renderPage(state, handlers));
  }

  async function loadReports(candidate) {
    const id = candidateId(candidate);
    const [replay, shadow] = await Promise.allSettled([
      candidate?.replay_report_id
        ? api.get(`${EVALUATION_ROOT}/replay-ab-runs/${endpointId(candidate.replay_report_id)}`)
        : Promise.resolve(null),
      candidate?.shadow_report_id
        ? api.get(`${EVALUATION_ROOT}/shadow-runs/${endpointId(candidate.shadow_report_id)}`)
        : Promise.resolve(null),
    ]);
    if (state.destroyed || candidateId(state.candidate) !== id) return;
    state.replay = replay.status === "fulfilled" ? firstRecord(replay.value) : null;
    state.shadow = shadow.status === "fulfilled" ? firstRecord(shadow.value) : null;
  }

  async function selectCandidate(id) {
    if (!id || state.destroyed) return;
    state.detailStatus = "loading";
    state.errorCode = null;
    state.candidate = state.candidates.find((item) => candidateId(item) === id) || null;
    state.replay = null;
    state.shadow = null;
    state.handoff = null;
    state.release = null;
    state.rollback = null;
    render();
    try {
      const detail = await api.get(`${CANDIDATE_ROOT}/${endpointId(id)}`);
      if (state.destroyed) return;
      state.candidate = firstRecord(detail);
      state.detailStatus = "ready";
      await loadReports(state.candidate);
      render();
    } catch (error) {
      if (state.destroyed) return;
      state.detailStatus = "error";
      state.errorCode = errorCode(error);
      render();
    }
  }

  async function refresh() {
    state.listStatus = "loading";
    state.errorCode = null;
    render();
    try {
      const payload = await api.get(CANDIDATE_ROOT);
      if (state.destroyed) return;
      state.candidates = listPayload(payload).map(firstRecord).filter((item) => candidateId(item));
      state.listStatus = "ready";
      const selected = candidateId(state.candidate);
      if (selected && state.candidates.some((item) => candidateId(item) === selected)) await selectCandidate(selected);
      else if (state.candidates[0]) await selectCandidate(candidateId(state.candidates[0]));
      else {
        state.candidate = null;
        state.detailStatus = "idle";
        render();
      }
    } catch (error) {
      if (state.destroyed) return;
      state.listStatus = "error";
      state.detailStatus = "idle";
      state.errorCode = errorCode(error);
      render();
    }
  }

  async function runEvaluation(kind) {
    const id = candidateId(state.candidate);
    if (!id || state.busyAction || state.destroyed) return;
    state.busyAction = kind;
    state.errorCode = null;
    render();
    try {
      const suffix = kind === "replay" ? "replay-ab-runs" : "shadow-runs";
      const result = await api.post(`${CANDIDATE_EVALUATION_ROOT}${endpointId(id)}/${suffix}`, {});
      const report = firstRecord(result);
      if (kind === "replay") state.replay = report;
      else state.shadow = report;
      state.section = kind;
      state.detailStatus = "ready";
    } catch (error) {
      state.errorCode = errorCode(error);
    } finally {
      state.busyAction = null;
      render();
    }
  }

  async function submitReview(approved) {
    const form = host.querySelector('[data-form="review"]');
    if (!form || !state.candidate || state.busyAction) return;
    const formData = new FormData(form);
    const reviewer = String(formData.get("reviewer") || "").trim();
    const note = String(formData.get("note") || "").trim();
    if (!reviewer || !note) {
      state.errorCode = "REVIEW_FIELDS_REQUIRED";
      render();
      return;
    }
    state.busyAction = "review";
    state.errorCode = null;
    render();
    try {
      const id = endpointId(candidateId(state.candidate));
      const endpoint = approved
        ? `${RELEASE_ROOT}/candidates/${id}/release`
        : `${CANDIDATE_ROOT}/${id}/reject`;
      const body = approved ? { reviewed_by: reviewer, note } : { note };
      const result = await api.post(endpoint, body);
      if (approved) {
        state.release = firstRecord(result);
        state.section = "release";
      } else {
        state.candidate = firstRecord(result);
      }
      state.detailStatus = "ready";
    } catch (error) {
      state.errorCode = errorCode(error);
    } finally {
      state.busyAction = null;
      render();
    }
  }

  async function handoff() {
    const id = candidateId(state.candidate);
    if (!id || state.busyAction) return;
    state.busyAction = "handoff";
    state.errorCode = null;
    render();
    try {
      state.handoff = await api.post(`${CANDIDATE_ROOT}/${endpointId(id)}/handoff`, {});
    } catch (error) {
      state.errorCode = errorCode(error);
    } finally {
      state.busyAction = null;
      render();
    }
  }

  async function rollback() {
    const form = host.querySelector('[data-form="rollback"]');
    if (!form || state.busyAction) return;
    const values = new FormData(form);
    const payload = {
      skill_id: String(values.get("skill_id") || "").trim(),
      target_version_id: String(values.get("target_version_id") || "").trim(),
      reviewed_by: String(values.get("reviewed_by") || "").trim(),
      note: String(values.get("note") || "").trim(),
    };
    if (Object.values(payload).some((value) => !value)) {
      state.errorCode = "ROLLBACK_FIELDS_REQUIRED";
      render();
      return;
    }
    state.busyAction = "rollback";
    state.errorCode = null;
    render();
    try {
      state.rollback = firstRecord(await api.post(`${RELEASE_ROOT}/rollback`, payload));
      state.section = "rollback";
    } catch (error) {
      state.errorCode = errorCode(error);
    } finally {
      state.busyAction = null;
      render();
    }
  }

  const handlers = {
    refresh,
    refreshSelected: () => selectCandidate(candidateId(state.candidate)),
    handoff,
    rollback,
  };

  async function handleClick(event) {
    const target = event.target.closest("[data-action]");
    if (!target || !host.contains(target)) return;
    const { action, candidateId: id, section } = target.dataset;
    if (action === "refresh-skills") await refresh();
    if (action === "select-candidate") await selectCandidate(id);
    if (action === "select-section") {
      state.section = section || "candidate";
      render();
    }
    if (action === "run-replay") await runEvaluation("replay");
    if (action === "run-shadow") await runEvaluation("shadow");
    if (action === "review-approve") await submitReview(true);
    if (action === "review-reject") await submitReview(false);
    if (action === "handoff") await handoff();
    if (action === "rollback") await rollback();
  }

  host.addEventListener("click", handleClick);
  refresh().catch(() => undefined);

  return Object.freeze({
    refresh,
    destroy: () => {
      state.destroyed = true;
      host.removeEventListener("click", handleClick);
      host.replaceChildren();
    },
  });
}
