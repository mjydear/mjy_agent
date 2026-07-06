# Athena Agent

```text
	 _   _   _                         _                    _
	/ \ | |_| |__   ___ _ __   __ _   / \   __ _  ___ _ __ | |_
   / _ \| __| '_ \ / _ \ '_ \ / _` | / _ \ / _` |/ _ \ '_ \| __|
  / ___ \ |_| | | |  __/ | | | (_| |/ ___ \ (_| |  __/ | | | |_
 /_/   \_\__|_| |_|\___|_| |_|\__,_/_/   \_\__, |\___|_| |_|\__|
										   |___/
```

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Tests](https://img.shields.io/badge/tests-124%20passed-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-%E2%89%A580%25-brightgreen)
![Framework Free](https://img.shields.io/badge/agent%20core-from%20scratch-orange)
![Cloud Native](https://img.shields.io/badge/deploy-Docker%20%7C%20K8s-informational)

Athena Agent 是一个从零实现、面向生产的自进化企业级智能助手：自研 ReAct Agent 核心、GEPA 自进化闭环、四层记忆系统、企业安全沙箱、全链路可观测性，以及云原生运维场景落地。

> **工程原则：真实实现 + 优雅降级。** 所有外部依赖（Redis、Milvus、LLM、K8s、Prometheus、云厂商 SDK、JWT）都走真实集成路径；当凭证或服务缺失时自动降级到内存 / 关键词 / mock 兜底，保证本地零配置可跑、生产接入即生效。

## 核心亮点

- 🧠 **自研 Agent Core**：执行循环、Prompt 组装、工具注册和记忆压缩均由项目直接实现，核心链路可解释、可测试、可替换。
- 🔁 **GEPA 自进化闭环**：执行轨迹 → 复杂度评分 → Skill 生成 → Skill 检索复用，沉淀可复用经验。
- 🧩 **四层记忆系统**：Working、Profile、Long-term、Skill Memory 分层管理上下文、画像、知识和技能。
- 🛡️ **企业级安全沙箱**：工具权限、路径边界、高危操作人工确认，降低自动化执行风险。
- 📈 **全链路可观测性**：Trace、Metrics、Web Console、Step Debugger，配套 Prometheus 指标、SLO 燃尽率告警与 Grafana 面板。
- 🏗️ **生产就绪高可用**：Redis 持久化、健康探针与优雅关闭、RBAC/JWT 鉴权、防篡改审计链、K8s HPA/PDB/Ingress 一站式落地。
- ☁️ **云原生运维场景**：K8s 故障排查、云成本优化、告警自动处置和资源巡检，真实 SDK 集成、缺凭证自动降级演示。

## 架构总览

```mermaid
flowchart LR
	User[Developer or SRE] --> CLI[CLI and Web Console]
	CLI --> Agent[ReAct Agent Core]
	Agent --> Prompt[Prompt Assembler]
	Agent --> Tools[Tool Registry]
	Agent --> Memory[Four Layer Memory]
	Tools --> Sandbox[Security Sandbox]
	Tools --> Cloud[CloudOps Tooling]
	Agent --> Trace[Tracer and Metrics]
	Trace --> GEPA[GEPA Self Evolution]
	GEPA --> Skills[Skill Library]
	Skills --> Prompt
	Cloud --> K8s[K8s Diagnosis]
	Cloud --> Cost[Cost Optimization]
	Cloud --> Alert[Alert Handling]
```

## 云场景闭环

```mermaid
flowchart TB
	Alert[Alertmanager Webhook] --> Parse[Alert Parser]
	Parse --> Workflow[Fault Diagnose Workflow]
	Workflow --> Snapshot[K8s and Cloud Snapshot]
	Snapshot --> Diagnose[Root Cause Analysis]
	Diagnose --> Guard{High Risk Operation?}
	Guard -->|No| Report[Recommendation Report]
	Guard -->|Yes| Confirm[Human Confirmation]
	Confirm --> Execute[Sandboxed Execution]
	Execute --> Knowledge[Ops Knowledge Base]
	Report --> Knowledge
```

## 生产就绪能力（Enterprise Readiness）

面向大厂生产标准落地的高可用与治理能力，全部有真实实现与测试覆盖：

| 维度 | 能力 | 关键实现 |
| --- | --- | --- |
| 🗄️ 状态持久化 | 会话 / 任务 / 指标 / 评测报告外置 Redis，重启不丢、多副本共享 | `athena/api/session_store.py`、`athena/api/task_store.py` |
| ❤️ 健康与发布 | `/healthz` 存活、`/readyz` 就绪 + Lifespan 优雅关闭（置 draining、排空任务、关连接） | `athena/api/routes/health.py`、`k8s/deployment.yaml`（preStop + 30s 优雅期） |
| 🔐 鉴权授权 | Scope 级 RBAC（`workflow:run` / `cloud:execute` / `benchmark:run` / `audit:read`）+ 可选 JWT Bearer；未配置凭证时默认放行保证向后兼容 | `athena/api/auth.py`、`athena/api/rbac.py` |
| 🧾 防篡改审计 | `sha256(prev_hash + payload)` 哈希链集中落库，`/api/audit/verify` 可对外证明完整性 | `athena/tools/audit_chain.py`、`athena/api/routes/audit.py` |
| 📊 SLO 与告警 | 可用性 99.5% / 延迟 P99<500ms，多窗口燃尽率告警（快烧 14.4x / 慢烧 6x）+ Alertmanager 抑制路由 + Grafana 面板 | `monitoring/slo.yaml`、`monitoring/alertmanager.yml`、`monitoring/grafana/athena-overview.json` |
| 🔁 告警闭环 | Alertmanager Webhook → 解析 → 写审计链，接自愈工作流入口 | `athena/api/routes/alerts.py` |
| 🧯 弹性容错 | 自研异步熔断器、退避重试、限流，LLM 依赖故障自动降级 | `athena/observability/incident.py`、中间件限流 |
| ☸️ 云原生编排 | Docker 多阶段非 root 镜像、K8s Deployment/HPA（CPU+自定义指标）/PDB/Ingress(TLS) | `Dockerfile`、`k8s/` |
| ✅ 质量保障 | 124 项测试通过，含针对真实 Redis 的集成测试与 Locust 压测脚本 | `tests/`、`tests/integration/`、`tests/load/locustfile.py` |

## 5 分钟快速开始

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
python -m pytest
athena chat "你好，请介绍一下 Athena Agent"
```

真实对话需要配置 `.env`：

```env
OPENAI_API_KEY=你的 API Key
ATHENA_LLM_MODEL=deepseek/deepseek-chat
```

Web Console：

```powershell
athena web --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000`。

## 功能特性

### 通用 Agent 能力

- ⚙️ ReAct 多步执行循环，支持工具调用、Observation 回填和最大步数保护。
- 🧰 装饰器式 Tool Registry，自动提取函数签名和工具描述。
- 🧠 Token 感知 Working Memory，支持重要性评分和裁剪策略。
- 🔎 Tree-sitter 代码结构解析，面向代码理解与测试生成场景。
- 📚 Skill Library 基于长期记忆做语义召回。
- 📊 Benchmark Engine 支持用例集、成功率和 Markdown 报告输出。

### 云运维能力

- ☸️ K8s CrashLoopBackOff / ImagePullBackOff / 资源压力诊断（Kubernetes SDK 真实拉取，缺集群自动降级 mock）。
- 💸 云成本优化：识别闲置实例并估算月度节省（阿里云 / AWS SDK 真实接入，缺凭证降级）。
- 🚨 告警自动处置：`/api/alerts/webhook` 接 Alertmanager，触发故障排查工作流并写审计链。
- 🔐 高危操作确认：重启实例等动作必须经过 confirmed 标记。
- 🧾 运维知识沉淀：故障工作流结果进入 Ops Knowledge Base，支持向量语义召回（嵌入缺失降级关键词）。

## Demo

| Demo | 场景 | 命令 |
| --- | --- | --- |
| Demo 1 | 代码智能助手：分析代码库 → 生成单元测试草案 | `python examples/demo1_code_analysis.py` |
| Demo 2 | 自进化演示：复杂任务 → 自动生成 Skill → 下次召回 | `python examples/demo2_self_evolution.py` |
| Demo 3 | 调试面板：轨迹、断点、Token 统计 | `python examples/demo3_debugger.py` |
| Demo 4 | K8s 故障自动排查 | `python examples/demo4_k8s_diagnose.py` |
| Demo 5 | 云成本智能优化 | `python examples/demo5_cost_optimize.py` |
| Demo 6 | 告警自动处置 | `python examples/demo6_alert_auto_handle.py` |

Demo 视频/GIF 建议录制后放入 `assets/`，README 中可替换为：

```markdown
![Athena Demo](assets/demo-overview.gif)
```

## 技术栈

- Python 3.11+
- LiteLLM 模型适配层
- Typer / Rich / Textual CLI 与 TUI
- FastAPI / Uvicorn Web Console
- Pydantic / SQLAlchemy 数据建模
- Redis 会话 / 任务 / 指标 / 审计持久化（内存后端自动降级）
- PyJWT + Scope RBAC 鉴权授权
- Prometheus / Grafana / Alertmanager 可观测性与 SLO 告警
- Tree-sitter 代码解析
- RestrictedPython 安全沙箱
- Milvus 适配边界与内存向量库 fallback
- Docker / Kubernetes（HPA / PDB / Ingress）云原生部署
- Kubernetes / Prometheus / 云厂商 SDK 真实集成与降级适配

## 测试与质量

- ✅ **124 项测试通过**，覆盖 Agent 核心、记忆、沙箱、API、鉴权、审计链、可观测性等模块。
- 🧪 **集成测试**：`tests/integration/` 对真实 Redis 做往返验证，缺依赖自动跳过（`pytest -m integration`）。
- 📈 **压测脚本**：`tests/load/locustfile.py` 模拟读写混合流量（`locust -f tests/load/locustfile.py`）。
- 🔍 **静态质量**：`black` / `isort` 格式化，`mypy` 类型检查，覆盖率红线 ≥ 80%。

```powershell
python -m pytest -q                       # 单元 + 集成（有 Redis 时）
python -m pytest -m "not integration" -q  # 仅单元测试（无外部依赖）
```


## 文档

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [GETTING_STARTED.md](GETTING_STARTED.md)
- [DEVELOPMENT.md](DEVELOPMENT.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)
- [API_REFERENCE.md](API_REFERENCE.md)
- [FAQ.md](FAQ.md)
- [docs/demos/demo_guide.md](docs/demos/demo_guide.md)
- [docs/benchmarks/performance_report.md](docs/benchmarks/performance_report.md)
- [docs/features/enterprise_landing_upgrade_plan.md](docs/features/enterprise_landing_upgrade_plan.md)
- [docs/interview/resume.md](docs/interview/resume.md)
- [docs/interview/questions.md](docs/interview/questions.md)
- [docs/interview/demo_script.md](docs/interview/demo_script.md)

## 质量红线

- 当前 `requirements.txt` 未引入 LangChain、AutoGen、LlamaIndex 等第三方 Agent 框架。
- 性能报告只记录可复现实测数据，未实测的对比项保留为待补录。
- 面试前必须本机跑通 6 个 demo、`pytest`、格式化、类型检查和覆盖率报告。

## 贡献指南

1. Fork 仓库并创建 feature 分支。
2. 安装依赖：`pip install -r requirements.txt && pip install -e .`。
3. 新增功能必须附带单元测试或 demo 验证路径。
4. 提交前运行 `black .`、`isort .`、`mypy athena examples tests`、`pytest`。
5. PR 描述写清楚变更动机、测试结果和潜在风险。

## Star History

项目公开后可替换为真实仓库地址：

```markdown
[![Star History Chart](https://api.star-history.com/svg?repos=yourname/athena-agent&type=Date)](https://star-history.com/#yourname/athena-agent&Date)
```
