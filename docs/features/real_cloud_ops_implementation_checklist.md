# 真实云运维 Agent 实现清单

> ## 📌 实现进度（截至 2026-07-08）
>
> - **阶段 0 基础确认**：已完成（入口链路 / mock 边界 / 配置入口 / 复用能力梳理完毕）。
> - **阶段 1 真实 K8s 只读诊断 MVP**：核心已完成并接入两条出口（tool registry + `/api/cloud-ops` k8s 模式）。
>   - 已实现只读能力：list namespaces / list pods / describe pod / list deployments / list services / list events / get pod logs / get node status。
>   - 已实现：`ops.mode`(mock/real)、kubeconfig/context/白名单/超时配置、白名单校验、自动降级 mock、证据型诊断器。
>   - **待补**：真实 kind/minikube 联调。
> - **阶段 2 结构化报告模型**：已完成（新增 `OpsFinding`/`OpsDiagnosisReport` schema，K8s 场景返回 `readonly_report` 结构化 JSON；Web 右侧面板按 summary / severity / findings / actions / raw evidence 分区展示；`EvidenceBoundReportSummarizer` 约束 LLM 只基于 report JSON 总结）。
> - **阶段 3 K8s 高频故障 Playbook**：已完成（CrashLoopBackOff / ImagePullBackOff / Pod Pending / Service 无法访问 / CPU Memory 异常均输出统一 `OpsFinding`）。
> - **阶段 4 Prometheus 真实指标接入**：核心已完成（`ops.prometheus` 配置、真实 HTTP 查询、mock/unavailable 降级、常用 PromQL 封装、报告区分 Prometheus 指标证据）。
> - **阶段 5 受控写操作与人工确认**：核心已完成（低风险 Deployment 写操作 preview/confirm/execute/verify，高风险动作拦截，失败状态与审计成功标记按真实操作结果计算）。
> - **阶段 6 Alertmanager 告警闭环**：核心已完成（webhook 接收、labels/annotations 解析、诊断任务转换、Playbook 选择、K8s/Prometheus 证据报告、故障工作流、告警历史与审计记录）。
> - **阶段 7 安全治理与公司级能力**：核心已完成（默认只读、resource/verb 白名单、blocked action、高危拦截、actor/scope 元数据、preview/verify、回滚建议、审计链、dev/staging/prod 隔离、prod 写保护）。
> - **阶段 8 Web 控制台增强**：核心已完成（真实/模拟状态、K8s context、namespace 范围、Prometheus 状态、结构化报告、原始证据展开、操作风险/确认/进度/结果/回滚建议、告警历史）。
> - **阶段 9 本地真实演示环境**：已完成（`deploy/kind-demo/` 提供 kind 集群配置与跨平台启动脚本、四类异常工作负载样例、本地 Prometheus 部署清单、Alertmanager mock webhook 示例与发送脚本、一键演示 README 含完整路径）。
> - **测试**：扩展 `tests/test_alerts_webhook.py`、`tests/test_cloud_prometheus.py`、`tests/test_k8s_readonly_client.py`、`tests/test_cloud_ops.py`、`tests/test_web_console.py`，覆盖新只读能力（deployments/services/nodes/endpoints）、结构化报告、证据约束摘要、Playbook 输出、Prometheus 查询/降级、K8s 写操作确认/执行/拦截/白名单、安全治理策略、Alertmanager 告警闭环、namespace 输入解析与 Web 静态资源渲染入口；**本次相关回归 66 passed**。
> - **产物**：`athena/tools/cloud/k8s/{actions,client,diagnose,report,tools}.py`、`athena/tools/cloud/prometheus.py`、`athena/integration/alert_webhook.py`、`athena/api/routes/alerts.py`、`athena/config.py`(OpsSettings/K8sSettings/PrometheusSettings/OpsSecuritySettings)、`config.yaml`(ops 段)、`athena/cli/main.py`、`athena/api/services.py`、`deploy/kind-demo/`(kind 配置/启动脚本/异常工作负载/Prometheus 清单/webhook 示例/一键演示 README)。

## 目标定位

Athena 当前的云运维模式已经具备 Web Console、API、Agent 执行链路、工具入口、安全沙箱、审计和可观测性等基础能力。下一步目标不是直接进入生产自动化运维，而是先实现一个可验证、可演示、风险可控的真实测试集群级云运维 Agent。

推荐目标：

- 真实读取 Kubernetes 测试集群状态。
- 真实查询 Prometheus 指标。
- 以只读诊断为默认能力。
- 写操作必须人工确认。
- 保留 mock / fallback，保证本地零配置仍可运行。
- 所有诊断结论必须有真实证据支撑。

## 实现原则

- 真实数据优先：诊断基于 K8s API、日志、事件、Prometheus 指标，不基于硬编码结论。
- 只读优先：第一阶段不做写操作，避免误操作风险。
- 证据驱动：LLM 只总结工具结果，不编造观测不到的事实。
- 安全默认：没有明确配置时走 mock 或只读模式。
- 渐进增强：从本地 kind / minikube 开始，再扩展到云上测试集群，最后再考虑生产级治理。

## 阶段 0：基础确认

- [x] 确认云运维模式入口链路：Web -> API -> Service -> Tool。
- [x] 梳理现有 K8s 工具实现，确认真实 Kubernetes client 与 mock fallback 的边界。
- [x] 梳理现有云运维模式：K8s 运维、资源巡检、故障排查、成本优化。
- [x] 确认配置加载入口：`config.yaml`、`.env`、`athena/config.py`。
- [x] 确认安全沙箱、人工确认、审计链当前可复用能力。
- [x] 确认 Web 右侧详情面板是否能展示结构化诊断证据。

## 阶段 1：真实 K8s 只读诊断 MVP

- [x] 新增运维配置项：`ops.mode = mock | real`。
- [x] 新增 Kubernetes 配置项：
  - [x] `kubeconfig`
  - [x] `context`
  - [x] `namespace_allowlist`
  - [x] `request_timeout_seconds`（实现字段名为 `timeout`）
- [x] 支持加载本地 kubeconfig。
- [x] 支持 in-cluster service account，为未来部署到 K8s 内做准备。（`load_incluster_config` 优先，失败回退 kubeconfig）
- [x] 实现 namespace 白名单校验。
- [x] 实现只读 K8s 查询工具：
  - [x] list namespaces
  - [x] list pods
  - [x] get pod detail / describe 信息
  - [x] list deployments
  - [x] list services
  - [x] list events
  - [x] get pod logs
  - [x] get node status
- [x] 无 kubeconfig、连接失败或权限不足时自动降级 mock 或返回清晰错误。
- [x] Web 云运维模式支持输入“诊断 default 命名空间”。（已支持从 task 解析 namespace：`namespace=xxx` / `ns xxx` / `xxx 命名空间`）
- [x] 返回真实 Pod、Event、Log 摘要。（`readonly_findings` 含事件+日志证据）
- [x] 为真实 K8s 查询补单元测试和 mock client 测试。

### 阶段 1 验收标准

- [ ] 能连接本地 `kind` 或 `minikube` 集群。
- [ ] 能读取真实 namespace、pod、event、log。
- [ ] 故意部署异常 Pod 后，Web 能返回真实异常证据。
- [x] 无 kubeconfig 时，本地 Web 仍可进入 mock 云运维模式。

## 阶段 2：结构化诊断报告模型

- [x] 定义 `OpsFinding`：
  - [x] `severity`
  - [x] `resource_kind`
  - [x] `resource_name`
  - [x] `namespace`
  - [x] `symptom`
  - [x] `evidence`
  - [x] `probable_causes`
  - [x] `recommended_actions`
- [x] 定义 `OpsDiagnosisReport`：
  - [x] `summary`
  - [x] `namespace`
  - [x] `findings`
  - [x] `metrics`
  - [x] `actions`
  - [x] `raw_evidence`
- [x] API 返回结构化 JSON，而不是只返回大段文本。（`/api/cloud-ops/run` 的 `data.readonly_report`）
- [x] Web 右侧详情面板结构化展示故障现象、证据、根因候选、风险等级、建议动作。
- [x] LLM 只负责基于 `OpsDiagnosisReport` 总结，不允许编造不存在的证据。
- [x] 没有证据时明确返回“证据不足”。

## 阶段 3：K8s 高频故障 Playbook

- [x] 实现 `CrashLoopBackOff` 诊断：
  - [x] 读取容器状态。
  - [x] 读取 restart count。
  - [x] 拉取最近日志。
  - [x] 读取相关 events。
- [x] 实现 `ImagePullBackOff` 诊断：
  - [x] 提取镜像名。
  - [x] 读取拉取失败事件。
  - [x] 输出 registry、tag、imagePullSecret 相关提示。
- [x] 实现 `Pod Pending` 诊断：
  - [x] 读取调度失败原因。
  - [x] 检查节点资源不足。
  - [x] 检查 nodeSelector、taint、toleration。
  - [x] 检查 PVC 绑定状态。
- [x] 实现 `Service 无法访问` 诊断：
  - [x] 检查 service selector。
  - [x] 检查 endpoints 是否为空。
  - [x] 检查 pod readiness。
  - [x] 检查 targetPort / containerPort 是否匹配。
- [x] 实现 `CPU / Memory 异常` 诊断：
  - [x] 接入 Prometheus 指标。
  - [x] 判断 CPU throttling、OOMKilled、memory pressure。
- [x] 每个 Playbook 输出统一 `OpsFinding`。
- [x] 每个 Playbook 都有测试样例。

## 阶段 4：Prometheus 真实指标接入

- [x] 新增 Prometheus 配置项：
  - [x] `enabled`
  - [x] `base_url`
  - [x] `timeout_seconds`
- [x] 实现 Prometheus query API client。
- [x] 支持常用 PromQL 查询：
  - [x] Pod CPU 使用率
  - [x] Pod Memory 使用量
  - [x] Pod 重启次数
  - [x] HTTP 5xx 错误率
  - [x] 请求延迟 P95 / P99
  - [x] 服务可用性
- [x] Prometheus 不可用时降级为“指标不可用”，不影响 K8s 诊断。
- [x] 报告中明确区分 K8s 证据、Prometheus 指标证据、日志证据。
- [x] 为 Prometheus client 补测试。

## 阶段 5：受控写操作与人工确认

- [x] 定义 `OpsAction`（实现名为 `K8sActionPlan` / `K8sActionResult`）：
  - [x] `action_type`
  - [x] `namespace`
  - [x] `resource_kind`
  - [x] `resource_name`
  - [x] `risk`
  - [x] `command_preview`
  - [x] `requires_confirmation`
- [x] 先支持低风险动作：
  - [x] rollout restart deployment
  - [x] scale deployment
  - [x] pause rollout
  - [x] resume rollout
- [x] 禁止高风险动作：
  - [x] delete namespace
  - [x] delete pvc
  - [x] patch secret
  - [x] 修改 RBAC
  - [x] 批量删除资源
- [x] Web 展示操作确认卡片。
- [x] 用户确认后才执行写操作。
- [x] 执行前做 namespace allowlist 校验。
- [x] 执行后读取资源状态验证结果。
- [x] 所有写操作写入审计链。
- [x] 写操作失败时展示真实错误信息。
- [x] 为写操作补安全测试。

## 阶段 6：Alertmanager 告警闭环

- [x] 接收 Alertmanager Webhook。
- [x] 解析告警 labels：
  - [x] `alertname`
  - [x] `namespace`
  - [x] `pod`
  - [x] `deployment`
  - [x] `severity`
- [x] 解析告警 annotations：
  - [x] `summary`
  - [x] `description`
- [x] 将告警转换为诊断任务。
- [x] 根据告警类型选择 Playbook。
- [x] 自动拉取 K8s 证据。
- [x] 自动拉取 Prometheus 指标。
- [x] 生成诊断报告。
- [x] Web Console 展示告警处理记录。
- [x] 审计记录告警来源和处理结果。
- [x] 支持手动 POST 告警用于本地测试。

## 阶段 7：安全治理与公司级能力

- [x] 默认只读模式。（`ops.security.default_readonly=true`；未识别写意图时只执行只读诊断）
- [x] namespace 白名单。
- [x] resource kind 白名单。
- [x] verb 白名单。
- [x] blocked action 黑名单。
- [x] 高危动作强制拦截。
- [x] 用户身份识别。（API 层 tenant actor 透传到 K8s action plan / security metadata）
- [x] RBAC scope 校验。（`/api/cloud-ops/run` 依赖 `require_scope("cloud:execute")`，action plan 标记 required_scope）
- [x] 操作前 preview。
- [x] 操作后 verify。
- [x] 回滚建议生成。
- [x] 审计链完整性校验。（CloudOps 写操作走统一 audit chain，可通过现有 verify 接口校验）
- [x] 多环境隔离：dev、staging、prod。
- [x] prod 环境默认禁止写操作，除非显式开启并二次确认。（`ATHENA_OPS_PROD_WRITE_ENABLED=true` + confirmed）

## 阶段 8：Web 控制台增强

- [x] 云运维模式增加真实 / 模拟状态标识。
- [x] 展示当前连接的 K8s context。
- [x] 展示当前 namespace 范围。
- [x] 展示 Prometheus 连接状态。
- [x] 诊断报告结构化展示。
- [x] 证据区可展开查看原始数据。
- [x] 建议操作区展示风险等级。
- [x] 写操作确认弹窗。
- [x] 操作执行进度。
- [x] 操作结果反馈。
- [x] 告警处理历史列表。

## 阶段 9：本地真实演示环境

- [x] 提供 kind 集群启动脚本。
- [x] 提供异常工作负载样例：
  - [x] crashloop app
  - [x] bad image app
  - [x] pending pod
  - [x] service selector mismatch
- [x] 提供本地 Prometheus 部署清单。
- [x] 提供 Alertmanager mock webhook 示例。
- [x] 提供一键演示文档。
- [x] 文档包含完整演示路径：
  - [x] 启动 kind。
  - [x] 部署异常应用。
  - [x] 启动 Athena。
  - [x] Web 发起诊断。
  - [x] 查看真实报告。
  - [x] 执行人工确认修复。

## 阶段 10：测试与验收

- [ ] 单元测试：
  - [ ] K8s client
  - [ ] Prometheus client
  - [ ] Playbook 判断逻辑
  - [ ] OpsFinding 输出
  - [ ] 安全策略拦截
- [ ] API 测试：
  - [ ] 诊断接口
  - [ ] 操作确认接口
  - [ ] 告警 webhook
- [ ] 集成测试：
  - [ ] kind 集群真实诊断
  - [ ] Prometheus 真实查询
  - [ ] Alertmanager 告警闭环
- [ ] Web 测试：
  - [ ] 云运维报告展示
  - [ ] 操作确认流程
  - [ ] 删除会话功能不回归
- [ ] 回归测试：
  - [ ] 无 kubeconfig 时 mock 模式仍可用
  - [ ] 无 Prometheus 时 K8s 诊断仍可用
  - [ ] 权限不足时返回清晰错误
  - [ ] 所有写操作必须确认

## 最小 MVP 勾选版

优先完成以下内容，就可以将项目从“云运维演示模式”推进到“真实 Kubernetes 测试集群只读诊断”。

- [x] `ops.mode = real`
- [x] 读取本地 kubeconfig
- [x] namespace 白名单
- [x] list pods
- [x] get events
- [x] get pod logs
- [x] 检测 CrashLoopBackOff
- [x] 检测 ImagePullBackOff
- [x] Web 输入“诊断 default 命名空间”
- [x] 返回结构化真实诊断报告
- [x] 无 kubeconfig 自动降级 mock

## 建议配置草案

```yaml
ops:
  mode: mock  # mock | real
  kubernetes:
    kubeconfig: "~/.kube/config"
    context: ""
    namespace_allowlist:
      - default
      - dev
      - staging
    request_timeout_seconds: 5
  prometheus:
    enabled: false
    base_url: "http://127.0.0.1:9090"
    timeout_seconds: 5
  safety:
    readonly_by_default: true
    require_confirmation: true
    blocked_verbs:
      - delete
      - patch_secret
      - delete_namespace
    allowed_write_actions:
      - restart_deployment
      - scale_deployment
```

## 推荐落地顺序

1. 先打通真实 K8s 只读工具。
2. 增加 Ops 配置和 mock / real 切换。
3. 定义统一诊断结果模型。
4. 实现 CrashLoopBackOff 和 ImagePullBackOff 两个 Playbook。
5. Web 云运维模式展示结构化报告。
6. 再接 Prometheus。
7. 最后做人工确认写操作和 Alertmanager 闭环。

## 结论

个人项目最适合先做到真实测试集群级：真实读取、真实诊断、只读为主、人工确认写操作。这条路线能保证项目足够真实，也能避免一开始就陷入生产级权限、安全、审批、回滚等复杂治理问题。
