/*
📦 模块名称：Athena Web Console 前端交互脚本
📍 架构位置：浏览器展示层，位于 HTML 页面和 FastAPI API 之间。
🎯 核心作用：管理页面状态、调用后端 API、渲染聊天消息、解析 SSE 流式事件和展示指标/轨迹/Benchmark。
🔗 依赖关系：依赖浏览器原生 DOM/fetch/ReadableStream API；被 index.html 通过 <script> 引入。
💡 设计思路：使用轻量“集中状态对象 + 函数式渲染”方式，不引入 React/Vue，保证打开页面即可运行。
📚 学习重点：重点看 state 如何保存 UI 状态、api() 如何封装请求、streamChat() 如何读取真实流式输出。
*/

/*
🔍 原理讲解：
这个前端文件可以理解成一个小型控制台应用：
用户点击/输入 → 修改 state → 调后端 API → 根据响应重新渲染 DOM。

举个例子：
输入 "检查服务" → sendMessage() → streamChat() → /api/chat/stream → handleSseChunk() → 聊天区和右侧轨迹更新。
*/

const state = {
  sessions: [], // 💡 学习提示：集中保存会话列表，避免多个 DOM 区域各自维护一份数据导致不同步。
  currentSessionId: null,
  mode: "chat",
  cloudMode: "k8s",
  pendingCloudConfirmation: null,
  activeTab: "trace",
  latestTaskId: null,
  traceEvents: [],
  benchmarkReport: "",
  abortController: null, // 💡 学习提示：AbortController 是浏览器取消 fetch 流请求的标准方式。
};

const elements = {
  // 💡 学习提示：启动时统一缓存 DOM 节点，后面函数直接复用，代码更短也更容易看出页面结构。
  newSession: document.getElementById("new-session"),
  sessionList: document.getElementById("session-list"),
  currentSession: document.getElementById("current-session"),
  modeTitle: document.getElementById("mode-title"),
  chatLog: document.getElementById("chat-log"),
  detailPanel: document.getElementById("detail-panel"),
  statusPill: document.getElementById("status-pill"),
  input: document.getElementById("message-input"),
  send: document.getElementById("send-message"),
  cancel: document.getElementById("cancel-stream"),
};

/**
 * 更新右上角运行状态。
 *
 * 功能说明：根据任务是否忙碌切换状态文字和颜色。
 * 参数说明：text 是显示文本；busy 表示是否正在执行任务。
 * 返回值：无，直接修改 DOM。
 * 设计思路：把状态样式集中在一个函数里，避免多个地方重复写 className。
 * 使用示例：setStatus("Streaming", true)
 */
function setStatus(text, busy = false) {
  elements.statusPill.textContent = text;
  elements.statusPill.className = busy
    ? "rounded-full bg-[#fff0d6] px-3 py-1 text-xs font-medium text-[#8a5a00]"
    : "rounded-full bg-[#e7f1e8] px-3 py-1 text-xs font-medium text-[#2d6a3f]";
}

/**
 * 封装 JSON API 请求。
 *
 * 功能说明：统一设置 Content-Type、解析 JSON、把非 2xx 响应转成 Error。
 * 参数说明：path 是接口路径；options 是 fetch 配置。
 * 返回值：后端返回的 JSON 对象。
 * 设计思路：所有普通 API 都走这里，错误处理和 header 不用到处复制。
 * 使用示例：const sessions = await api("/api/sessions")
 */
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ message: response.statusText })); // 💡 学习提示：错误响应不一定是 JSON，所以 catch 做兜底。
    throw new Error(error.message || "Request failed");
  }
  return response.json();
}

/**
 * 创建新会话。
 *
 * 功能说明：调用后端创建 session，并刷新左侧会话列表和聊天区域。
 * 参数说明：无。
 * 返回值：Promise<void>。
 * 设计思路：前端不自己生成 session_id，统一由后端生成，避免 id 冲突。
 * 使用示例：await createSession()
 */
async function createSession() {
  const payload = await api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title: `Athena Session ${state.sessions.length + 1}` }),
  });
  state.currentSessionId = payload.session.session_id;
  await loadSessions();
  renderMessages(payload.session.messages);
}

/**
 * 加载会话列表。
 *
 * 功能说明：从后端读取所有活跃会话，并重新渲染左侧列表。
 * 参数说明：无。
 * 返回值：Promise<void>。
 * 设计思路：列表以服务端状态为准，避免前端缓存和后端过期清理不一致。
 * 使用示例：await loadSessions()
 */
async function loadSessions() {
  state.sessions = await api("/api/sessions");
  renderSessions();
}

/**
 * 切换当前会话。
 *
 * 功能说明：按 sessionId 读取会话详情，并渲染对应历史消息。
 * 参数说明：sessionId 是后端返回的会话 id。
 * 返回值：Promise<void>。
 * 设计思路：点击会话时再拉取详情，列表刷新不携带完整消息，页面更轻。
 * 使用示例：await selectSession("session-xxx")
 */
async function selectSession(sessionId) {
  state.currentSessionId = sessionId;
  const session = await api(`/api/sessions/${sessionId}`);
  renderSessions();
  renderMessages(session.messages);
}

/**
 * 渲染左侧会话列表。
 *
 * 功能说明：根据 state.sessions 创建会话按钮，并标记当前选中的会话。
 * 参数说明：无，读取全局 state。
 * 返回值：无，直接修改 DOM。
 * 设计思路：每次重渲染前清空容器，MVP 场景下最直观；会话特别多时可再做局部更新。
 * 使用示例：renderSessions()
 */
function renderSessions() {
  elements.sessionList.innerHTML = "";
  state.sessions.forEach((session) => {
    const button = document.createElement("button");
    button.className = `session-card ${session.session_id === state.currentSessionId ? "active" : ""}`;
    button.innerHTML = `<div class="font-semibold text-sm">${escapeHtml(session.title)}</div><div class="mt-1 text-xs text-[#6c5f4d]">${session.message_count} 条消息</div>`; // 💡 学习提示：用户可控文本进 innerHTML 前必须 escapeHtml，防止脚本注入。
    button.addEventListener("click", () => selectSession(session.session_id));
    elements.sessionList.appendChild(button);
  });
  const current = state.sessions.find((session) => session.session_id === state.currentSessionId);
  elements.currentSession.textContent = current ? current.title : "尚未创建会话";
}

/**
 * 渲染聊天历史。
 *
 * 功能说明：清空聊天区，然后按消息顺序重新创建气泡。
 * 参数说明：messages 是后端返回的消息数组。
 * 返回值：无。
 * 设计思路：会话切换时全量重绘最简单，不需要手动比较新旧消息差异。
 * 使用示例：renderMessages(session.messages)
 */
function renderMessages(messages) {
  elements.chatLog.innerHTML = "";
  messages.forEach((message) => addMessage(message.role, message.content));
}

/**
 * 添加单条消息气泡。
 *
 * 功能说明：把用户/助手/思考消息渲染到聊天区。
 * 参数说明：role 是 user 或 assistant；content 是文本；variant 控制样式类型。
 * 返回值：创建出来的 bubble DOM 节点，流式输出会持续更新它。
 * 设计思路：streamChat 需要先创建一个空助手气泡，再随着 SSE 事件填入最终答案。
 * 使用示例：const bubble = addMessage("assistant", "", "assistant")
 */
function addMessage(role, content, variant = role) {
  const row = document.createElement("div");
  row.className = `message-row ${role === "user" ? "user" : ""}`;
  const bubble = document.createElement("div");
  bubble.className = `message-bubble ${variant}`;
  bubble.textContent = content; // 💡 学习提示：普通消息用 textContent，浏览器会自动当文本处理，比 innerHTML 更安全。
  row.appendChild(bubble);
  elements.chatLog.appendChild(row);
  elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
  return bubble;
}

/**
 * 根据当前模式发送任务。
 *
 * 功能说明：读取输入框内容，并按 chat/workflow/benchmark 三种模式分发。
 * 参数说明：无。
 * 返回值：Promise<void>。
 * 设计思路：同一个输入框服务三种能力，用户体验像一个统一控制台。
 * 使用示例：点击发送按钮时调用 sendMessage()
 */
async function sendMessage() {
  const message = elements.input.value.trim();
  if (!message) return;
  if (!state.currentSessionId) await createSession(); // 💡 学习提示：没有会话时自动创建，避免用户必须先点“新建会话”。
  elements.input.value = "";
  addMessage("user", message, "user");
  if (state.mode === "workflow") {
    await runWorkflow(message);
  } else if (state.mode === "cloud") {
    await runCloudOps(message);
  } else if (state.mode === "benchmark") {
    await runBenchmark();
  } else {
    await streamChat(message);
  }
  await loadSessions();
}

/**
 * 执行云运维场景。
 *
 * 功能说明：根据 state.cloudMode 调用 CloudOps API，展示答案和轨迹；高危任务支持二次确认。
 * 参数说明：task 是用户输入，空时后端会使用场景默认任务；confirmed 表示是否已经人工确认。
 * 返回值：Promise<void>。
 * 设计思路：前端只负责传 mode/task，具体 K8s、资源、故障、成本逻辑全部在后端服务层。
 * 使用示例：await runCloudOps("KubePodCrashLooping")
 */
async function runCloudOps(task, confirmed = false) {
  setStatus("CloudOps", true);
  const response = await api("/api/cloud-ops/run", {
    method: "POST",
    body: JSON.stringify({ mode: state.cloudMode, task, provider: "aliyun", confirmed }),
  });
  state.latestTaskId = response.task_id;
  state.traceEvents = response.steps.map((step) => ({ event_type: step.event_type, content: step.content, step_index: step.step_index }));
  if (response.requires_confirmation) {
    state.pendingCloudConfirmation = { task, mode: state.cloudMode, provider: "aliyun" };
    addCloudConfirmation(response.answer);
  } else {
    state.pendingCloudConfirmation = null;
    addMessage("assistant", response.answer, "assistant");
  }
  state.activeTab = "trace";
  renderTabs();
  renderDetailPanel();
  setStatus("Ready");
}

/**
 * 渲染高危操作确认按钮。
 *
 * 功能说明：展示后端的风险提示，并提供 confirmed=true 的二次提交入口。
 * 参数说明：message 是后端返回的风险说明。
 * 返回值：无。
 * 设计思路：确认状态存在前端 state 中，避免用户点错子模式后确认到另一个任务。
 * 使用示例：addCloudConfirmation("重启实例属于高危操作")
 */
function addCloudConfirmation(message) {
  const bubble = addMessage("assistant", message, "thought");
  const button = document.createElement("button");
  button.className = "confirmation-button mt-3";
  button.textContent = "确认执行";
  button.addEventListener("click", async () => {
    const pending = state.pendingCloudConfirmation;
    if (!pending) return;
    button.disabled = true;
    state.cloudMode = pending.mode;
    await runCloudOps(pending.task, true);
  });
  bubble.appendChild(document.createElement("br"));
  bubble.appendChild(button);
}

/**
 * 执行真实 SSE 流式对话。
 *
 * 功能说明：POST 到 /api/chat/stream，读取 ReadableStream，把每个 SSE chunk 交给 handleSseChunk。
 * 参数说明：message 是用户输入。
 * 返回值：Promise<void>。
 * 设计思路：因为接口是 POST，不能直接用 EventSource，所以用 fetch + reader 手动解析流。
 * 使用示例：await streamChat("检查服务")
 *
 * 🎯 面试考点：为什么不用 EventSource？答案：标准 EventSource 主要发 GET 请求，这里需要 POST 传 session_id 和 message。
 */
async function streamChat(message) {
  setStatus("Streaming", true);
  state.traceEvents = [];
  state.abortController = new AbortController();
  elements.cancel.disabled = false;
  const assistantBubble = addMessage("assistant", "", "assistant");
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.currentSessionId, message }),
      signal: state.abortController.signal,
    });
    if (!response.ok || !response.body) throw new Error("Stream request failed");
    const reader = response.body.getReader(); // 💡 学习提示：ReadableStream reader 可以一小块一小块读响应，而不是等完整响应结束。
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n"); // 💡 学习提示：SSE 用空行分隔事件，所以按两个换行切块。
      buffer = chunks.pop() || ""; // 💡 学习提示：最后一段可能是不完整事件，要留到下次数据到达再解析。
      chunks.forEach((chunk) => handleSseChunk(chunk, assistantBubble));
    }
    setStatus("Ready");
  } catch (error) {
    if (error.name !== "AbortError") addMessage("assistant", error.message, "thought"); // 💡 学习提示：用户主动取消不算错误，不需要再显示红色错误消息。
    setStatus("Ready");
  } finally {
    elements.cancel.disabled = true;
    state.abortController = null;
    renderDetailPanel();
  }
}

/**
 * 解析一段 SSE 文本并更新页面。
 *
 * 功能说明：从 `data: {...}` 中取出 JSON，更新轨迹列表或最终答案气泡。
 * 参数说明：chunk 是一条 SSE 文本；assistantBubble 是当前助手回复气泡。
 * 返回值：无。
 * 设计思路：SSE 传输格式和 UI 状态更新分开写，streamChat 只负责读流，这里负责理解事件。
 * 使用示例：handleSseChunk('data: {"event_type":"final"}', bubble)
 *
 * 🔍 原理讲解：
 * 后端发送 `data: {"event_type":"step","content":"..."}\n\n`。
 * 前端先找 data 行 → JSON.parse → 根据 event_type 决定是放进轨迹，还是更新最终答案。
 */
function handleSseChunk(chunk, assistantBubble) {
  const line = chunk.split("\n").find((item) => item.startsWith("data:"));
  if (!line) return;
  const event = JSON.parse(line.slice(5).trim()); // 💡 学习提示：slice(5) 是去掉 "data:" 前缀，这是 SSE 文本协议的固定格式。
  state.latestTaskId = event.task_id || state.latestTaskId;
  if (["step", "start", "task"].includes(event.event_type)) {
    state.traceEvents.push(event);
  }
  if (event.event_type === "final" || event.event_type === "done") {
    assistantBubble.textContent = event.content;
  }
  renderDetailPanel();
}

/**
 * 执行多 Agent 工作流。
 *
 * 功能说明：调用 /api/workflow/run，把响应结果渲染成消息和右侧轨迹。
 * 参数说明：task 是用户输入的复杂任务。
 * 返回值：Promise<void>。
 * 设计思路：工作流目前是同步返回，所以不需要像 SSE 那样手动读取流。
 * 使用示例：await runWorkflow("收集日志; 验证修复")
 */
async function runWorkflow(task) {
  setStatus("Workflow", true);
  const response = await api("/api/workflow/run", {
    method: "POST",
    body: JSON.stringify({ session_id: state.currentSessionId, task, workflow_type: "plan_execute" }),
  });
  state.latestTaskId = response.task_id;
  state.traceEvents = response.steps.map((step) => ({ event_type: step.event_type, content: step.content, step_index: step.step_index }));
  addMessage("assistant", response.answer, "assistant");
  setStatus("Ready");
  renderDetailPanel();
}

/**
 * 执行 Benchmark 评测。
 *
 * 功能说明：调用 /api/benchmark/run，把 Markdown 报告显示在聊天区和 Benchmark Tab。
 * 参数说明：无，当前固定使用 web-console case_set。
 * 返回值：Promise<void>。
 * 设计思路：Benchmark 是独立演示模式，所以发送按钮在该模式下不使用输入文本内容。
 * 使用示例：await runBenchmark()
 */
async function runBenchmark() {
  setStatus("Benchmark", true);
  const response = await api("/api/benchmark/run", {
    method: "POST",
    body: JSON.stringify({ case_set: "web-console" }),
  });
  state.benchmarkReport = response.report;
  addMessage("assistant", response.report, "assistant");
  setStatus("Ready");
  state.activeTab = "benchmark"; // 💡 学习提示：运行完评测自动切到报告 Tab，用户不需要再手动找结果。
  renderTabs();
  renderDetailPanel();
}

/**
 * 刷新右侧性能指标。
 *
 * 功能说明：调用 /api/metrics，并把数值渲染成指标卡片。
 * 参数说明：无。
 * 返回值：Promise<void>。
 * 设计思路：指标 Tab 每次打开时按需请求，避免页面启动就拉所有数据。
 * 使用示例：await refreshMetrics()
 */
async function refreshMetrics() {
  const metrics = await api("/api/metrics");
  elements.detailPanel.innerHTML = `<div class="grid gap-3">
    ${metricCard("总任务数", metrics.total_tasks)}
    ${metricCard("成功率", `${(metrics.success_rate * 100).toFixed(1)}%`)}
    ${metricCard("平均耗时", `${metrics.average_duration_seconds.toFixed(3)}s`)}
    ${metricCard("Token 消耗", metrics.token_usage)}
    <pre>${escapeHtml(JSON.stringify(metrics.error_distribution, null, 2))}</pre>
  </div>`;
}

/**
 * 生成一个指标卡片 HTML。
 *
 * 功能说明：把 label/value 转成统一样式的小卡片。
 * 参数说明：label 是指标名；value 是展示值。
 * 返回值：HTML 字符串。
 * 设计思路：提取重复 HTML，避免 refreshMetrics 里堆太多模板代码。
 * 使用示例：metricCard("成功率", "100%")
 */
function metricCard(label, value) {
  return `<div class="trace-item"><div class="text-xs text-[#6c5f4d]">${label}</div><div class="mt-1 text-2xl font-semibold text-[#17324d]">${value}</div></div>`;
}

/**
 * 渲染右侧详情面板。
 *
 * 功能说明：根据 activeTab 展示轨迹、指标或 Benchmark 报告。
 * 参数说明：无，读取 state.activeTab。
 * 返回值：无。
 * 设计思路：三个 Tab 共用同一个容器，切换时整体替换内容最直接。
 * 使用示例：renderDetailPanel()
 */
function renderDetailPanel() {
  if (state.activeTab === "metrics") {
    refreshMetrics();
    return;
  }
  if (state.activeTab === "benchmark") {
    elements.detailPanel.innerHTML = state.benchmarkReport ? `<pre>${escapeHtml(state.benchmarkReport)}</pre>` : `<p class="text-sm text-[#6c5f4d]">尚未运行 Benchmark。</p>`;
    return;
  }
  if (!state.traceEvents.length) {
    elements.detailPanel.innerHTML = `<p class="text-sm text-[#6c5f4d]">当前任务还没有轨迹。</p>`;
    return;
  }
  elements.detailPanel.innerHTML = state.traceEvents.map((event) => `<details class="trace-item mb-3" open>
    <summary class="cursor-pointer text-sm font-semibold text-[#17324d]">#${event.step_index || 0} ${escapeHtml(event.event_type)}</summary>
    <pre class="mt-3">${escapeHtml(formatMaybeJson(event.content))}</pre>
  </details>`).join("");
}

/**
 * 更新 Tab 选中样式。
 *
 * 功能说明：根据 state.activeTab 给按钮添加或移除 active 类。
 * 参数说明：无。
 * 返回值：无。
 * 设计思路：样式状态从 state 推导，避免按钮样式和真实状态不一致。
 * 使用示例：renderTabs()
 */
function renderTabs() {
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === state.activeTab);
  });
}

/**
 * 切换控制台访问模式。
 *
 * 功能说明：在单 Agent、工作流、Benchmark 三种模式之间切换。
 * 参数说明：mode 是 chat/workflow/benchmark。
 * 返回值：无。
 * 设计思路：模式只改变发送逻辑和标题，不需要切换页面，演示更顺滑。
 * 使用示例：setMode("workflow")
 */
function setMode(mode) {
  state.mode = mode;
  const titles = { chat: "单 Agent 对话", workflow: "多 Agent 工作流", benchmark: "Benchmark 评测", cloud: "云运维模式" };
  elements.modeTitle.textContent = titles[mode];
  document.querySelectorAll(".mode-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
}

/**
 * 切换云运维子模式。
 *
 * 功能说明：在 K8s、资源巡检、故障排查、成本优化之间切换。
 * 参数说明：cloudMode 是 k8s/resource/fault/cost。
 * 返回值：无。
 * 设计思路：子模式只影响 CloudOps API 的 mode 参数，不影响其他对话模式。
 * 使用示例：setCloudMode("fault")
 */
function setCloudMode(cloudMode) {
  state.cloudMode = cloudMode;
  document.querySelectorAll(".cloud-mode-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.cloudMode === cloudMode);
  });
  if (state.mode !== "cloud") setMode("cloud");
}

/**
 * 尝试格式化 JSON 文本。
 *
 * 功能说明：如果字符串是 JSON，就缩进格式化；否则原样返回。
 * 参数说明：value 是待展示内容。
 * 返回值：格式化后的字符串。
 * 设计思路：工具输出可能是 JSON，也可能是普通文本，try/catch 可以兼容两者。
 * 使用示例：formatMaybeJson('{"ok":true}')
 */
function formatMaybeJson(value) {
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

/**
 * 转义 HTML 特殊字符。
 *
 * 功能说明：把用户或后端返回文本变成安全可插入 innerHTML 的字符串。
 * 参数说明：value 是任意待展示值。
 * 返回值：转义后的文本。
 * 设计思路：凡是要进入 innerHTML 的外部文本都必须转义，防止 XSS。
 * 使用示例：escapeHtml('<script>alert(1)</script>')
 *
 * 🎯 面试考点：为什么有些地方用 textContent，有些地方用 escapeHtml + innerHTML？答案：模板里需要 HTML 结构时必须 innerHTML，但动态文本必须先转义。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

elements.newSession.addEventListener("click", createSession); // 💡 学习提示：事件绑定统一放在文件末尾，便于先读函数定义再看页面如何接线。
elements.send.addEventListener("click", sendMessage);
elements.cancel.addEventListener("click", () => state.abortController?.abort());
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault(); // 💡 学习提示：阻止 textarea 默认换行，才能实现 Enter 发送、Shift+Enter 换行。
    sendMessage();
  }
});
document.querySelectorAll(".mode-button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
document.querySelectorAll(".cloud-mode-button").forEach((button) => button.addEventListener("click", () => setCloudMode(button.dataset.cloudMode)));
document.querySelectorAll(".tab-button").forEach((button) => button.addEventListener("click", () => {
  state.activeTab = button.dataset.tab;
  renderTabs();
  renderDetailPanel();
}));

createSession().catch((error) => {
  setStatus("Error");
  addMessage("assistant", error.message, "thought");
});

/*
🤔 思考题：

1. 如果要把会话历史保存到浏览器 localStorage，你会放在哪个函数里？
2. 当前 SSE 解析只处理 data 行，如果后端发送 event/id 字段，需要怎么扩展？
3. 如果 traceEvents 很多，renderDetailPanel 每次全量 innerHTML 会有什么性能问题？
4. 为什么 escapeHtml 对前端安全很重要？你能举一个攻击例子吗？
5. ⚡ 优化建议：未来可以把 API、状态管理、渲染函数拆成多个 JS 文件，页面复杂后更容易维护。
*/