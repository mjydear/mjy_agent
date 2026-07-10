# Athena 本地真实云运维演示环境（kind）

本目录提供一套**一键本地演示环境**，用于把 Athena 从「云运维演示模式（mock）」推进到
「真实 Kubernetes 集群只读诊断（real）」，并演示告警闭环与人工确认修复。

> 适用范围：本地开发 / 面试演示 / 联调。**不要**用于生产集群。

## 目录结构

| 文件 | 用途 |
| --- | --- |
| `kind-cluster.yaml` | kind 集群配置（控制面 + 1 worker，映射 30090/30093 端口） |
| `start-kind.sh` / `start-kind.ps1` | 一键创建集群 + 部署异常应用 + Prometheus |
| `workloads/crashloop-app.yaml` | 异常样例：CrashLoopBackOff |
| `workloads/bad-image-app.yaml` | 异常样例：ImagePullBackOff |
| `workloads/pending-pod.yaml` | 异常样例：Pending / 不可调度 |
| `workloads/service-selector-mismatch.yaml` | 异常样例：Service selector 不匹配（无 Endpoints） |
| `prometheus.yaml` | 本地最小化 Prometheus（NodePort 30090） |
| `alertmanager-webhook-example.json` | mock Alertmanager 告警 payload |
| `send-alert.sh` / `send-alert.ps1` | 向 Athena 发送 mock 告警 |

## 前置依赖

- [Docker](https://www.docker.com/)
- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- Athena 本地依赖（`venv` 已安装 `requirements.txt`）

---

## 完整演示路径

### 1. 启动 kind 集群 + 部署异常应用

Windows PowerShell：

```powershell
pwsh -File deploy/kind-demo/start-kind.ps1
```

Linux / macOS：

```bash
bash deploy/kind-demo/start-kind.sh
```

脚本会：创建 `athena-demo` 集群 → 建命名空间 `athena-demo` → 部署 4 个异常工作负载 → 部署 Prometheus。

验证异常已就绪：

```bash
kubectl get pods -n athena-demo
# 期望看到 crashloop-app（CrashLoopBackOff）、bad-image-app（ImagePullBackOff）、pending-pod（Pending）
kubectl get endpoints web-backend -n athena-demo
# 期望 ENDPOINTS 为 <none>（selector 不匹配）
```

### 2. 以 real 模式启动 Athena

编辑 [config.yaml](../../config.yaml) 的 `ops` 段（或用环境变量覆盖）：

```yaml
ops:
  mode: real                      # 关键：切换到真实集群
  kubernetes:
    kubeconfig: null              # 用默认 ~/.kube/config（kind 已写入）
    context: kind-athena-demo     # kind 创建的 context 名
    namespace_allowlist: [athena-demo, default]
    timeout: 10.0
  prometheus:
    enabled: true                 # 打开真实 Prometheus 查询
    base_url: http://localhost:30090
```

> 安全边界：`namespace_allowlist` 之外的命名空间会**硬失败**（越权不降级）；
> 只有基础设施故障（无 kubeconfig / 连接失败）才会自动降级回 mock。

用环境变量覆盖（免改文件）示例（PowerShell）：

```powershell
$env:ATHENA_OPS_MODE = "real"
$env:ATHENA_OPS_K8S_CONTEXT = "kind-athena-demo"
```

启动 Web 服务：

```powershell
d:/mjy-agent/venv/Scripts/python.exe -m athena.cli web
# 或按项目实际入口：uvicorn athena.api.server:app --port 8000
```

### 3. Web 发起诊断

打开浏览器访问 <http://localhost:8000>，在对话框输入：

```
诊断 athena-demo 命名空间
```

在「对话轨迹」右侧详情面板可看到：

- **云环境状态卡片**：mode=real、k8s context、namespace、Prometheus 可用性。
- **结构化诊断报告**：逐条 `OpsFinding`（CrashLoopBackOff / ImagePullBackOff / Pending / Service 无 Endpoints），含证据与建议。

### 4. 查看真实报告（命令行方式）

```bash
curl -sS -X POST http://localhost:8000/api/cloud-ops/run \
  -H "Content-Type: application/json" \
  -d '{"query": "诊断 athena-demo 命名空间", "namespace": "athena-demo"}'
```

### 5. 告警闭环（mock Alertmanager）

向 Athena 推送一条 mock 告警，触发自动诊断并写入审计链：

Windows：

```powershell
pwsh -File deploy/kind-demo/send-alert.ps1
```

Linux / macOS：

```bash
bash deploy/kind-demo/send-alert.sh
```

查看告警历史：`GET /api/alerts/history`（Web 控制台「告警记录」标签页亦可查看）。

### 6. 执行人工确认修复（受控写操作）

对可修复项（如需要重启的 Deployment），Web 会给出**预览 → 确认 → 执行 → 校验**流程。
只有白名单动作（`rollout_restart` / `scale` / `pause` / `resume`）且低风险才允许，
且 `prod` 环境默认禁止写（需 `ATHENA_OPS_PROD_WRITE_ENABLED=true` 显式开启）。

示例（重启 crashloop-app，需二次确认）：

```bash
# 1) 预览
curl -sS -X POST http://localhost:8000/api/cloud-ops/run \
  -H "Content-Type: application/json" \
  -d '{"query": "重启 athena-demo 命名空间的 crashloop-app", "namespace": "athena-demo"}'
# 返回 preview + confirm token；带上 confirm 再次调用即执行并校验。
```

---

## 清理

```bash
kind delete cluster --name athena-demo
```

## 运行真实集群 E2E 测试

`tests/test_k8s_e2e.py` 直连本 kind 集群验证 real 链路（默认跳过，需显式开启）：

```bash
# 1) 先启动集群与异常工作负载（见上文步骤 1）
bash deploy/kind-demo/start-kind.sh
# 2) 开启 E2E 并指向真实集群
ATHENA_E2E_K8S=1 ATHENA_OPS_MODE=real \
  ATHENA_OPS_K8S_CONTEXT=kind-athena-demo \
  ATHENA_OPS_K8S_NAMESPACE_ALLOWLIST=athena-demo,default \
  pytest tests/test_k8s_e2e.py
```

未设 `ATHENA_E2E_K8S` 时整文件 skip，不影响 CI。

## 生产加固配置（SRE 规范）

| 目的 | 配置 | 说明 |
| --- | --- | --- |
| 暴露真实故障 | `ATHENA_OPS_STRICT_REAL=true` | real 连不上集群时直接报错，不静默降级 mock（避免掩盖故障）；降级发生时前端云状态卡片会标注「降级 mock」 |
| webhook 鉴权 | `ATHENA_OPS_WEBHOOK_SECRET=<secret>` | Alertmanager webhook 必须携带 `X-Alert-Secret` 头且匹配，否则 401；未设则不强制（演示/CI 兼容） |
| 审计持久化 | `ATHENA_REDIS_URL=<url>` + `ATHENA_OPS_REQUIRE_DURABLE_AUDIT=true` | 强制审计哈希链落 Redis；未配置 Redis 时启动即报错，防止内存后端重启丢审计 |

发送带密钥的告警（配置了 webhook_secret 后）：

```bash
curl -sS -X POST http://localhost:8000/api/alerts/webhook \
  -H "Content-Type: application/json" \
  -H "X-Alert-Secret: <secret>" \
  -d @deploy/kind-demo/alertmanager-webhook-example.json
```

## 回归与降级验证要点

- 无 kubeconfig / 集群不可达 → 自动降级 mock，诊断仍可用（`strict_real=true` 时改为直接报错）。
- `prometheus.enabled=false` 或 Prometheus 不可达 → K8s 诊断照常，报告中结构化标注 Prometheus 不可用。
- 请求 `namespace_allowlist` 之外的命名空间 → 返回清晰的越权错误（不降级）。
- 所有写操作必须人工确认后才执行。
