const TASK_ID = "task-demo-checkout-042";
const CANDIDATE_ID = "skill-candidate-payment-07";

const task = {
  task_id: TASK_ID,
  goal: "定位 checkout-service 支付失败的根因并保留关键证据",
  status: "succeeded",
  profile: "COMPLEX",
  tick_count: 4,
  budget: { mode: "bounded", consumed_tokens: 2840, remaining_tokens: 7160 },
  execution: {
    backend: "ecommerce-adapter",
    decision_mode: "react",
    memory_strategy: "working + episodic + semantic + skill",
  },
  report: "支付请求被风控策略拒绝，订单状态未推进。建议复核风控命中规则并引导用户重试。",
};

const events = [
  { event_id: "evt-01", sequence: 1, type: "task.created", created_at: "2026-08-20T09:14:02Z", data: { summary: "任务进入 Runtime" } },
  { event_id: "evt-02", sequence: 2, type: "tick.started", created_at: "2026-08-20T09:14:03Z", data: { summary: "编译四层上下文，选择复杂任务路由" } },
  { event_id: "evt-03", sequence: 3, type: "tool.called", created_at: "2026-08-20T09:14:04Z", data: { summary: "调用 order.lookup", tool_name: "order.lookup" } },
  { event_id: "evt-04", sequence: 4, type: "tool.succeeded", created_at: "2026-08-20T09:14:05Z", data: { summary: "读取订单和支付事件证据" } },
  { event_id: "evt-05", sequence: 5, type: "task.succeeded", created_at: "2026-08-20T09:14:07Z", data: { summary: "生成结构化诊断报告" } },
];

const evidence = [
  { claim: "支付事件记录为 RISK_REJECTED", source_ref: "payment_events/evt-7842", confidence: 0.98, artifact_id: "artifact-payment-01" },
  { claim: "订单仍处于 PAYMENT_PENDING", source_ref: "orders/order-1024", confidence: 0.99, artifact_id: "artifact-order-01" },
  { claim: "未执行写操作，工具范围为只读", source_ref: "runtime/tool-gateway", confidence: 1, artifact_id: "artifact-safety-01" },
];

const usage = {
  total_tokens: 2840,
  entries: [
    { purpose: "任务复杂度判断", model: "deepseek-chat-lite", total_tokens: 380, route_reason: "简单路由决策" },
    { purpose: "ReAct 决策", model: "deepseek-chat", total_tokens: 2140, route_reason: "复杂任务 + 证据整合" },
    { purpose: "结果结构化", model: "deepseek-chat-lite", total_tokens: 320, route_reason: "短输出优先轻量模型" },
  ],
};

const context = {
  working: ["订单 order-1024", "当前状态 PAYMENT_PENDING", "待确认支付事件"],
  retrieved: [{ layer: "episodic", score: 0.91, summary: "相似支付拒绝任务的证据保留策略" }],
  compacted: { original_tokens: 6120, compiled_tokens: 1680, saved_tokens: 4440 },
};

const candidate = {
  id: CANDIDATE_ID,
  name: "Payment rejection evidence-first",
  status: "candidate",
  evaluation_status: "rejected",
  environment_type: "ecommerce-replay",
  risk_level: "low",
  version: "v0.3-candidate",
  activation_allowed: false,
  allowed_tools: ["order.lookup", "payment.lookup", "event.lookup"],
  trigger: { domain: "payment", signal: "status=failed" },
  replay_report_id: "replay-demo-12",
  shadow_report_id: "shadow-demo-01",
};

const replay = {
  status: "rejected",
  gate: { activation_allowed: false, failed_checks: ["total_token_growth"] },
  aggregate: {
    baseline: { task_success_rate: 0.6667, evidence_retention_rate: 1, average_tick_count: 2.167, average_tool_call_count: 1.5, average_total_tokens: 1692.583, average_latency_ms: 322.382, safety_violations: 0 },
    candidate: { task_success_rate: 0.6667, evidence_retention_rate: 1, average_tick_count: 2.167, average_tool_call_count: 1.5, average_total_tokens: 1822.583, average_latency_ms: 312.291, safety_violations: 0 },
    delta: { task_success_rate: 0, evidence_retention_rate: 0, average_tick_count: 0, average_tool_call_count: 0, average_total_tokens: 130, average_latency_ms: -10.091, safety_violations: 0 },
  },
  comparisons: [
    { case_id: "payment-01", baseline: { task_success: true, evidence_retention: 1, tick_count: 2, tool_call_count: 1, input_tokens: 1450, total_tokens: 1600, latency_ms: 310, safety_violations: 0 }, candidate: { task_success: true, evidence_retention: 1, tick_count: 2, tool_call_count: 1, input_tokens: 1580, total_tokens: 1730, latency_ms: 298, safety_violations: 0 } },
    { case_id: "payment-02", baseline: { task_success: false, evidence_retention: 1, tick_count: 3, tool_call_count: 2, input_tokens: 1658, total_tokens: 1785, latency_ms: 335, safety_violations: 0 }, candidate: { task_success: false, evidence_retention: 1, tick_count: 3, tool_call_count: 2, input_tokens: 1788, total_tokens: 1915, latency_ms: 320, safety_violations: 0 } },
  ],
};

const shadow = { status: "observed", summary: { sample_count: 48, safety_violations: 0, candidate_read_count: 48, behavior_change_rate: 0.08 } };

function response(value) {
  return Promise.resolve(value);
}

export function createDemoApiClient() {
  return Object.freeze({
    get(path) {
      if (path === "/api/runtime/tasks") return response({ items: [task] });
      if (path.endsWith(`/api/runtime/tasks/${TASK_ID}`)) return response(task);
      if (path.includes("/events")) return response({ items: events, next_cursor: "evt-05" });
      if (path.includes("/evidence")) return response({ items: evidence });
      if (path.includes("/context")) return response(context);
      if (path.includes("/usage")) return response(usage);
      if (path === "/api/skill-candidates") return response({ items: [candidate] });
      if (path.includes("/api/skill-candidates/")) return response(candidate);
      if (path.includes("replay-ab-runs")) return response(replay);
      if (path.includes("shadow-runs")) return response(shadow);
      return response({});
    },
    post(path) {
      if (path.includes("replay-ab-runs")) return response(replay);
      if (path.includes("shadow-runs")) return response(shadow);
      if (path === "/api/runtime/tasks") return response(task);
      return response({ ok: true });
    },
    patch: () => response({ ok: true }),
    delete: () => response({ ok: true }),
  });
}
