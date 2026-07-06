# 真实云运维 Agent 实现清单

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

- [ ] 确认云运维模式入口链路：Web -> API -> Service -> Tool。
- [ ] 梳理现有 K8s 工具实现，确认真实 Kubernetes client 与 mock fallback 的边界。
- [ ] 梳理现有云运维模式：K8s 运维、资源巡检、故障排查、成本优化。
- [ ] 确认配置加载入口：`config.yaml`、`.env`、`athena/config.py`。
- [ ] 确认安全沙箱、人工确认、审计链当前可复用能力。
- [ ] 确认 Web 右侧详情面板是否能展示结构化诊断证据。

## 阶段 1：真实 K8s 只读诊断 MVP

- [ ] 新增运维配置项：`ops.mode = mock | real`。
- [ ] 新增 Kubernetes 配置项：
  - [ ] `kubeconfig`
  - [ ] `context`
  - [ ] `namespace_allowlist`
  - [ ] `request_timeout_seconds`
- [ ] 支持加载本地 kubeconfig。
- [ ] 支持 in-cluster service account，为未来部署到 K8s 内做准备。
- [ ] 实现 namespace 白名单校验。
- [ ] 实现只读 K8s 查询工具：
  - [ ] list namespaces
  - [ ] list pods
  - [ ] get pod detail / describe 信息
  - [ ] list deployments
  - [ ] list services
  - [ ] list events
  - [ ] get pod logs
  - [ ] get node status
- [ ] 无 kubeconfig、连接失败或权限不足时自动降级 mock 或返回清晰错误。
- [ ] Web 云运维模式支持输入“诊断 default 命名空间”。
- [ ] 返回真实 Pod、Event、Log 摘要。
- [ ] 为真实 K8s 查询补单元测试和 mock client 测试。

### 阶段 1 验收标准

- [ ] 能连接本地 `kind` 或 `minikube` 集群。
- [ ] 能读取真实 namespace、pod、event、log。
- [ ] 故意部署异常 Pod 后，Web 能返回真实异常证据。
- [ ] 无 kubeconfig 时，本地 Web 仍可进入 mock 云运维模式。

## 阶段 2：结构化诊断报告模型

- [ ] 定义 `OpsFinding`：
  - [ ] `severity`
  - [ ] `resource_kind`
  - [ ] `resource_name`
  - [ ] `namespace`
  - [ ] `symptom`
  - [ ] `evidence`
  - [ ] `probable_causes`
  - [ ] `recommended_actions`
- [ ] 定义 `OpsDiagnosisReport`：
  - [ ] `summary`
  - [ ] `namespace`
  - [ ] `findings`
  - [ ] `metrics`
  - [ ] `actions`
  - [ ] `raw_evidence`
- [ ] API 返回结构化 JSON，而不是只返回大段文本。
- [ ] Web 右侧详情面板结构化展示故障现象、证据、根因候选、风险等级、建议动作。
- [ ] LLM 只负责基于 `OpsDiagnosisReport` 总结，不允许编造不存在的证据。
- [ ] 没有证据时明确返回“证据不足”。

## 阶段 3：K8s 高频故障 Playbook

- [ ] 实现 `CrashLoopBackOff` 诊断：
  - [ ] 读取容器状态。
  - [ ] 读取 restart count。
  - [ ] 拉取最近日志。
  - [ ] 读取相关 events。
- [ ] 实现 `ImagePullBackOff` 诊断：
  - [ ] 提取镜像名。
  - [ ] 读取拉取失败事件。
  - [ ] 输出 registry、tag、imagePullSecret 相关提示。
- [ ] 实现 `Pod Pending` 诊断：
  - [ ] 读取调度失败原因。
  - [ ] 检查节点资源不足。
  - [ ] 检查 nodeSelector、taint、toleration。
  - [ ] 检查 PVC 绑定状态。
- [ ] 实现 `Service 无法访问` 诊断：
  - [ ] 检查 service selector。
  - [ ] 检查 endpoints 是否为空。
  - [ ] 检查 pod readiness。
  - [ ] 检查 targetPort / containerPort 是否匹配。
- [ ] 实现 `CPU / Memory 异常` 诊断：
  - [ ] 接入 Prometheus 指标。
  - [ ] 判断 CPU throttling、OOMKilled、memory pressure。
- [ ] 每个 Playbook 输出统一 `OpsFinding`。
- [ ] 每个 Playbook 都有测试样例。

## 阶段 4：Prometheus 真实指标接入

- [ ] 新增 Prometheus 配置项：
  - [ ] `enabled`
  - [ ] `base_url`
  - [ ] `timeout_seconds`
- [ ] 实现 Prometheus query API client。
- [ ] 支持常用 PromQL 查询：
  - [ ] Pod CPU 使用率
  - [ ] Pod Memory 使用量
  - [ ] Pod 重启次数
  - [ ] HTTP 5xx 错误率
  - [ ] 请求延迟 P95 / P99
  - [ ] 服务可用性
- [ ] Prometheus 不可用时降级为“指标不可用”，不影响 K8s 诊断。
- [ ] 报告中明确区分 K8s 证据、Prometheus 指标证据、日志证据。
- [ ] 为 Prometheus client 补测试。

## 阶段 5：受控写操作与人工确认

- [ ] 定义 `OpsAction`：
  - [ ] `action_type`
  - [ ] `namespace`
  - [ ] `resource_kind`
  - [ ] `resource_name`
  - [ ] `risk`
  - [ ] `command_preview`
  - [ ] `requires_confirmation`
- [ ] 先支持低风险动作：
  - [ ] rollout restart deployment
  - [ ] scale deployment
  - [ ] pause rollout
  - [ ] resume rollout
- [ ] 禁止高风险动作：
  - [ ] delete namespace
  - [ ] delete pvc
  - [ ] patch secret
  - [ ] 修改 RBAC
  - [ ] 批量删除资源
- [ ] Web 展示操作确认卡片。
- [ ] 用户确认后才执行写操作。
- [ ] 执行前做 namespace allowlist 校验。
- [ ] 执行后读取资源状态验证结果。
- [ ] 所有写操作写入审计链。
- [ ] 写操作失败时展示真实错误信息。
- [ ] 为写操作补安全测试。

## 阶段 6：Alertmanager 告警闭环

- [ ] 接收 Alertmanager Webhook。
- [ ] 解析告警 labels：
  - [ ] `alertname`
  - [ ] `namespace`
  - [ ] `pod`
  - [ ] `deployment`
  - [ ] `severity`
- [ ] 解析告警 annotations：
  - [ ] `summary`
  - [ ] `description`
- [ ] 将告警转换为诊断任务。
- [ ] 根据告警类型选择 Playbook。
- [ ] 自动拉取 K8s 证据。
- [ ] 自动拉取 Prometheus 指标。
- [ ] 生成诊断报告。
- [ ] Web Console 展示告警处理记录。
- [ ] 审计记录告警来源和处理结果。
- [ ] 支持手动 POST 告警用于本地测试。

## 阶段 7：安全治理与公司级能力

- [ ] 默认只读模式。
- [ ] namespace 白名单。
- [ ] resource kind 白名单。
- [ ] verb 白名单。
- [ ] blocked action 黑名单。
- [ ] 高危动作强制拦截。
- [ ] 用户身份识别。
- [ ] RBAC scope 校验。
- [ ] 操作前 preview。
- [ ] 操作后 verify。
- [ ] 回滚建议生成。
- [ ] 审计链完整性校验。
- [ ] 多环境隔离：dev、staging、prod。
- [ ] prod 环境默认禁止写操作，除非显式开启并二次确认。

## 阶段 8：Web 控制台增强

- [ ] 云运维模式增加真实 / 模拟状态标识。
- [ ] 展示当前连接的 K8s context。
- [ ] 展示当前 namespace 范围。
- [ ] 展示 Prometheus 连接状态。
- [ ] 诊断报告结构化展示。
- [ ] 证据区可展开查看原始数据。
- [ ] 建议操作区展示风险等级。
- [ ] 写操作确认弹窗。
- [ ] 操作执行进度。
- [ ] 操作结果反馈。
- [ ] 告警处理历史列表。

## 阶段 9：本地真实演示环境

- [ ] 提供 kind 集群启动脚本。
- [ ] 提供异常工作负载样例：
  - [ ] crashloop app
  - [ ] bad image app
  - [ ] pending pod
  - [ ] service selector mismatch
- [ ] 提供本地 Prometheus 部署清单。
- [ ] 提供 Alertmanager mock webhook 示例。
- [ ] 提供一键演示文档。
- [ ] 文档包含完整演示路径：
  - [ ] 启动 kind。
  - [ ] 部署异常应用。
  - [ ] 启动 Athena。
  - [ ] Web 发起诊断。
  - [ ] 查看真实报告。
  - [ ] 执行人工确认修复。

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

- [ ] `ops.mode = real`
- [ ] 读取本地 kubeconfig
- [ ] namespace 白名单
- [ ] list pods
- [ ] get events
- [ ] get pod logs
- [ ] 检测 CrashLoopBackOff
- [ ] 检测 ImagePullBackOff
- [ ] Web 输入“诊断 default 命名空间”
- [ ] 返回结构化真实诊断报告
- [ ] 无 kubeconfig 自动降级 mock

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
