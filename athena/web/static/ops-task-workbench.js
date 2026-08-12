/* P1 OpsTask workbench. Task detail remains authoritative; SSE carries increments. */
(function () {
  "use strict";

  const TERMINAL_STATUSES = new Set(["succeeded", "failed", "cancelled"]);
  const NAMED_EVENTS = [
    "task.created",
    "task.started",
    "task.input_received",
    "tool.finished",
    "task.completed",
    "task.failed",
    "task.cancelled",
  ];
  const EVENT_LABELS = {
    "task.created": "任务已创建",
    "task.started": "开始采集",
    "task.input_received": "补充输入已接收",
    "tool.finished": "只读工具完成",
    "task.completed": "任务已完成",
    "task.failed": "任务失败",
    "task.cancelled": "任务已取消",
  };
  const STATUS_LABELS = {
    queued: "排队中",
    running: "运行中",
    waiting: "等待中",
    succeeded: "已成功",
    failed: "已失败",
    cancelled: "已取消",
  };

  const state = {
    initialized: false,
    available: false,
    active: false,
    tasks: [],
    task: null,
    evidence: [],
    events: [],
    taskId: null,
    lastSequence: 0,
    selectionToken: 0,
    source: null,
    reconnectTimer: null,
    refreshTimer: null,
    reconnectAttempt: 0,
  };

  const elements = {};

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(path, { ...options, headers });
    const text = await response.text();
    let payload = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        throw new Error("服务返回了无法解析的响应");
      }
    }
    if (!response.ok) {
      throw new Error(payload.message || payload.detail || "请求失败");
    }
    return Object.prototype.hasOwnProperty.call(payload, "data")
      ? payload.data
      : payload;
  }

  function cacheElements() {
    elements.root = document.getElementById("ops-task-workbench");
    elements.form = document.getElementById("ops-task-create");
    elements.objective = document.getElementById("ops-task-objective");
    elements.environment = document.getElementById("ops-task-environment");
    elements.namespace = document.getElementById("ops-task-namespace");
    elements.submit = document.getElementById("ops-task-submit");
    elements.error = document.getElementById("ops-task-error");
    elements.refresh = document.getElementById("ops-task-refresh");
    elements.connection = document.getElementById("ops-task-connection");
    elements.list = document.getElementById("ops-task-list");
    elements.count = document.getElementById("ops-task-count");
    elements.detail = document.getElementById("ops-task-detail");
  }

  function bindEvents() {
    elements.form.addEventListener("submit", createTask);
    elements.refresh.addEventListener("click", refreshWorkbench);
    elements.list.addEventListener("click", (event) => {
      const button = event.target.closest("[data-ops-task-id]");
      if (button) openTask(button.dataset.opsTaskId);
    });
    elements.detail.addEventListener("click", (event) => {
      if (event.target.closest("#ops-task-cancel")) cancelCurrentTask();
    });
  }

  async function init() {
    if (state.initialized) return state.available;
    cacheElements();
    if (!elements.root) return false;
    state.initialized = true;
    bindEvents();
    try {
      await refreshTaskList();
      state.available = true;
    } catch {
      state.available = false;
    }
    return state.available;
  }

  async function setActive(active) {
    state.active = Boolean(active);
    if (!state.active) {
      closeEventStream();
      clearTimers();
      setConnection("已暂停", "idle");
      return;
    }
    if (!state.initialized || !state.available) return;
    try {
      await refreshTaskList();
      const nextTaskId = state.taskId || state.tasks[0]?.id;
      if (nextTaskId) await openTask(nextTaskId);
      else renderEmptyDetail();
    } catch (error) {
      showError(error.message);
    }
  }

  async function refreshWorkbench() {
    elements.refresh.disabled = true;
    showError("");
    try {
      await refreshTaskList();
      if (state.taskId) {
        await refreshCurrentFacts(state.taskId, state.selectionToken);
      } else if (state.tasks[0]) {
        await openTask(state.tasks[0].id);
      }
    } catch (error) {
      showError(error.message);
    } finally {
      elements.refresh.disabled = false;
    }
  }

  async function refreshTaskList() {
    const result = await request("/api/ops/tasks");
    state.tasks = Array.isArray(result.items) ? [...result.items].reverse() : [];
    renderTaskList();
    return state.tasks;
  }

  async function createTask(event) {
    event.preventDefault();
    if (!elements.form.reportValidity()) return;
    setFormBusy(true);
    showError("");
    try {
      const task = await request("/api/ops/tasks", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey("create") },
        body: JSON.stringify({
          objective: elements.objective.value.trim(),
          environment_id: elements.environment.value.trim(),
          namespace: elements.namespace.value.trim(),
        }),
      });
      elements.objective.value = "";
      await refreshTaskList();
      await openTask(task.id);
    } catch (error) {
      showError(error.message);
    } finally {
      setFormBusy(false);
    }
  }

  async function openTask(taskId) {
    if (!taskId) return;
    closeEventStream();
    clearTimers();
    state.taskId = taskId;
    state.task = state.tasks.find((task) => task.id === taskId) || null;
    state.evidence = [];
    state.events = [];
    state.lastSequence = 0;
    state.reconnectAttempt = 0;
    const token = ++state.selectionToken;
    renderTaskList();
    renderLoadingDetail();
    try {
      await refreshCurrentFacts(taskId, token);
      if (!isCurrentSelection(taskId, token) || !state.active) return;
      connectEventStream(taskId, token, state.lastSequence);
    } catch (error) {
      if (isCurrentSelection(taskId, token)) {
        showError(error.message);
        renderDetail();
      }
    }
  }

  async function refreshCurrentFacts(taskId, token) {
    await refreshTaskDetail(taskId, token);
    if (!isCurrentSelection(taskId, token)) return null;
    await refreshTaskEvidence(taskId, token);
    return state.task;
  }

  async function refreshTaskDetail(taskId, token) {
    const task = await request(`/api/ops/tasks/${encodeURIComponent(taskId)}`);
    if (!isCurrentSelection(taskId, token)) return null;
    state.task = task;
    const index = state.tasks.findIndex((item) => item.id === task.id);
    if (index >= 0) state.tasks[index] = task;
    else state.tasks.unshift(task);
    renderTaskList();
    renderDetail();
    return task;
  }

  async function refreshTaskEvidence(taskId, token) {
    const result = await request(
      `/api/ops/tasks/${encodeURIComponent(taskId)}/evidence`
    );
    if (!isCurrentSelection(taskId, token)) return;
    state.evidence = Array.isArray(result.items) ? result.items : [];
    renderDetail();
  }

  function connectEventStream(taskId, token, afterSequence, options = {}) {
    if (!state.active || !isCurrentSelection(taskId, token)) return;
    closeEventStream();
    const cursor = Math.max(0, Number(afterSequence) || 0);
    const path = `/api/ops/tasks/${encodeURIComponent(taskId)}/events?after_seq=${cursor}`;
    const source = new EventSource(path);
    state.source = source;
    setConnection(options.stopAfterReplay ? "同步剩余事件" : "正在连接", "busy");

    source.addEventListener("open", () => {
      if (state.source !== source) return;
      state.reconnectAttempt = 0;
      setConnection("实时更新", "live");
    });
    NAMED_EVENTS.forEach((eventType) => {
      source.addEventListener(eventType, (event) => {
        handleNamedEvent(eventType, event, taskId, token, source);
      });
    });
    source.addEventListener("error", () => {
      if (state.source !== source) return;
      source.close();
      state.source = null;
      if (options.stopAfterReplay) {
        setConnection("任务已结束", "idle");
        return;
      }
      recoverAfterDisconnect(taskId, token);
    });
  }

  function handleNamedEvent(eventType, event, taskId, token, source) {
    if (state.source !== source || !isCurrentSelection(taskId, token)) return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    const sequence = Number(payload.sequence || event.lastEventId || 0);
    if (!Number.isFinite(sequence) || sequence <= state.lastSequence) return;
    state.lastSequence = sequence;
    state.events.push({
      type: eventType,
      sequence,
      data: payload.data && typeof payload.data === "object" ? payload.data : {},
      createdAt: payload.created_at || "",
    });
    renderDetail();
    scheduleFactRefresh(taskId, token);
    if (["task.completed", "task.failed", "task.cancelled"].includes(eventType)) {
      closeEventStream();
      setConnection("任务已结束", "idle");
    }
  }

  function scheduleFactRefresh(taskId, token) {
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = window.setTimeout(async () => {
      state.refreshTimer = null;
      try {
        await refreshCurrentFacts(taskId, token);
      } catch (error) {
        if (isCurrentSelection(taskId, token)) showError(error.message);
      }
    }, 120);
  }

  function recoverAfterDisconnect(taskId, token) {
    if (!state.active || !isCurrentSelection(taskId, token)) return;
    setConnection("连接中断，正在恢复", "busy");
    window.clearTimeout(state.reconnectTimer);
    const delay = Math.min(500 * 2 ** state.reconnectAttempt, 5000);
    state.reconnectAttempt += 1;
    state.reconnectTimer = window.setTimeout(async () => {
      state.reconnectTimer = null;
      try {
        // Detail is authoritative. Reconcile it before resuming persisted events.
        const task = await refreshTaskDetail(taskId, token);
        if (!task || !isCurrentSelection(taskId, token) || !state.active) return;
        await refreshTaskEvidence(taskId, token);
        if (!isCurrentSelection(taskId, token) || !state.active) return;
        connectEventStream(taskId, token, state.lastSequence, {
          stopAfterReplay: TERMINAL_STATUSES.has(task.status),
        });
      } catch {
        recoverAfterDisconnect(taskId, token);
      }
    }, delay);
  }

  async function cancelCurrentTask() {
    if (!state.task || TERMINAL_STATUSES.has(state.task.status)) return;
    if (!window.confirm("确定取消当前故障任务吗？已落库的事件和证据仍会保留。")) {
      return;
    }
    const button = document.getElementById("ops-task-cancel");
    if (button) button.disabled = true;
    showError("");
    try {
      state.task = await request(
        `/api/ops/tasks/${encodeURIComponent(state.task.id)}/cancel`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey("cancel") },
        }
      );
      renderDetail();
      await refreshTaskList();
    } catch (error) {
      showError(error.message);
      renderDetail();
    }
  }

  function renderTaskList() {
    if (!elements.list) return;
    elements.count.textContent = String(state.tasks.length);
    if (!state.tasks.length) {
      elements.list.innerHTML = '<p class="ops-task-list-empty">暂无故障任务</p>';
      return;
    }
    elements.list.innerHTML = state.tasks
      .map((task) => {
        const selected = task.id === state.taskId;
        return `<button type="button" class="ops-task-list-item${selected ? " active" : ""}" data-ops-task-id="${escapeHtml(task.id)}" aria-current="${selected ? "true" : "false"}">
          <span class="ops-task-list-title">${escapeHtml(task.objective || "未命名任务")}</span>
          <span class="ops-task-list-meta"><span class="ops-task-status ops-task-status--${safeTone(task.status)}">${escapeHtml(statusLabel(task.status))}</span><span>${escapeHtml(task.phase || "-")}</span></span>
        </button>`;
      })
      .join("");
  }

  function renderLoadingDetail() {
    elements.detail.innerHTML =
      '<div class="ops-task-empty" role="status">正在加载任务事实...</div>';
  }

  function renderEmptyDetail() {
    state.task = null;
    state.taskId = null;
    state.evidence = [];
    state.events = [];
    elements.detail.innerHTML =
      '<div class="ops-task-empty">创建或选择任务后，可在此查看事实状态。</div>';
    setConnection("未连接", "idle");
  }

  function renderDetail() {
    const task = state.task;
    if (!task) {
      renderEmptyDetail();
      return;
    }
    const terminal = TERMINAL_STATUSES.has(task.status);
    const degradation = degradationStatus(task, state.evidence, state.events);
    const namespace = task.scope?.namespace || "-";
    elements.detail.innerHTML = `<div class="ops-task-detail-head">
      <div class="ops-task-detail-copy">
        <p class="ops-task-id">${escapeHtml(task.id)}</p>
        <h4>${escapeHtml(task.objective || "未命名任务")}</h4>
      </div>
      <button id="ops-task-cancel" type="button" class="btn btn-secondary ops-task-cancel" ${terminal ? "disabled" : ""} aria-label="取消当前故障任务">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
        <span>${terminal ? "不可取消" : "取消任务"}</span>
      </button>
    </div>
    <dl class="ops-task-facts">
      ${factItem("Environment Mode", String(task.environment_mode || "unknown").toUpperCase())}
      ${factItem("Task Phase", task.phase || "-")}
      ${factItem("Status", statusLabel(task.status), `ops-task-status--${safeTone(task.status)}`)}
      ${factItem("Namespace", namespace)}
    </dl>
    <div class="ops-task-degradation ops-task-degradation--${degradation.tone}" role="status">
      <span>降级状态</span><strong>${escapeHtml(degradation.label)}</strong><small>${escapeHtml(degradation.detail)}</small>
    </div>
    <div class="ops-task-streams">
      <section class="ops-task-stream-section" aria-labelledby="ops-task-events-heading">
        <div class="ops-task-section-heading"><h5 id="ops-task-events-heading">阶段事件</h5><span>${state.events.length}</span></div>
        <ol class="ops-task-events" aria-live="polite">${renderEvents()}</ol>
      </section>
      <section class="ops-task-stream-section" aria-labelledby="ops-task-evidence-heading">
        <div class="ops-task-section-heading"><h5 id="ops-task-evidence-heading">Evidence 摘要</h5><span>${state.evidence.length}</span></div>
        <div class="ops-task-evidence-list">${renderEvidence()}</div>
      </section>
    </div>`;
  }

  function factItem(label, value, className = "") {
    return `<div class="ops-task-fact"><dt>${escapeHtml(label)}</dt><dd class="${className}">${escapeHtml(value)}</dd></div>`;
  }

  function renderEvents() {
    if (!state.events.length) {
      return '<li class="ops-task-stream-empty">等待持久化事件...</li>';
    }
    return state.events
      .map((event) => `<li class="ops-task-event">
        <span class="ops-task-event-marker" aria-hidden="true"></span>
        <div><strong>${escapeHtml(EVENT_LABELS[event.type] || event.type)}</strong><p>${escapeHtml(eventSummary(event))}</p><time>${escapeHtml(formatTime(event.createdAt))} · #${event.sequence}</time></div>
      </li>`)
      .join("");
  }

  function eventSummary(event) {
    const data = event.data || {};
    if (event.type === "tool.finished") {
      return `${data.action || "只读工具"} · ${data.status || "unknown"} · ${Array.isArray(data.evidence_refs) ? data.evidence_refs.length : 0} evidence`;
    }
    if (event.type === "task.failed") return data.error_code || "执行失败";
    if (data.phase) return `阶段 ${data.phase}`;
    return "事实事件已持久化";
  }

  function renderEvidence() {
    if (!state.evidence.length) {
      return '<p class="ops-task-stream-empty">尚未采集 Evidence。</p>';
    }
    return state.evidence
      .map((item) => `<article class="ops-task-evidence-item">
        <div class="ops-task-evidence-head"><strong>${escapeHtml(item.source || item.type || "Evidence")}</strong><span class="ops-task-origin ops-task-origin--${safeTone(item.data_origin)}">${escapeHtml(String(item.data_origin || "unknown").toUpperCase())}</span></div>
        <p>${escapeHtml(item.summary || "无摘要")}</p>
        <small>${escapeHtml(item.type || "-")} · ${escapeHtml(item.id || "-")}</small>
      </article>`)
      .join("");
  }

  function degradationStatus(task, evidence, events) {
    const mode = String(task.environment_mode || "unknown").toLowerCase();
    const explicitFallback = events.some((event) => {
      const data = event.data || {};
      return data.degraded === true || data.fallback === true || data.fallback_mode;
    });
    const nonLiveEvidence = evidence.some(
      (item) => String(item.data_origin || "").toLowerCase() !== "live"
    );
    if (mode === "live" && nonLiveEvidence) {
      return {
        tone: "danger",
        label: "来源异常",
        detail: "LIVE 任务出现非 LIVE Evidence，请停止使用结论。",
      };
    }
    if (explicitFallback) {
      return {
        tone: "warning",
        label: "已降级",
        detail: "执行事件声明了降级或 fallback。",
      };
    }
    if (mode === "mock") {
      return {
        tone: "mock",
        label: "显式 Mock",
        detail: "当前任务仅用于演练，不代表生产环境事实。",
      };
    }
    return {
      tone: "healthy",
      label: "未降级",
      detail: evidence.length ? "Evidence 来源与环境模式一致。" : "等待首条 Evidence。",
    };
  }

  function statusLabel(status) {
    return STATUS_LABELS[status] || status || "未知";
  }

  function safeTone(value) {
    const tone = String(value || "unknown").toLowerCase();
    return /^[a-z0-9_-]+$/.test(tone) ? tone : "unknown";
  }

  function setFormBusy(busy) {
    elements.submit.disabled = busy;
    elements.submit.textContent = busy ? "正在创建..." : "创建只读任务";
  }

  function setConnection(label, tone) {
    if (!elements.connection) return;
    elements.connection.textContent = label;
    elements.connection.dataset.tone = tone;
  }

  function showError(message) {
    if (elements.error) elements.error.textContent = message || "";
  }

  function closeEventStream() {
    if (state.source) state.source.close();
    state.source = null;
  }

  function clearTimers() {
    window.clearTimeout(state.reconnectTimer);
    window.clearTimeout(state.refreshTimer);
    state.reconnectTimer = null;
    state.refreshTimer = null;
  }

  function isCurrentSelection(taskId, token) {
    return state.taskId === taskId && state.selectionToken === token;
  }

  function idempotencyKey(action) {
    const random = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    return `web-ops-${action}-${random}`;
  }

  function formatTime(value) {
    if (!value) return "时间未知";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  window.AthenaOpsTaskWorkbench = { init, setActive, openTask };
})();
