# API 文档

## 会话

### `POST /api/sessions`

创建独立会话。

### `GET /api/sessions`

列出当前会话。

## 对话

### `POST /api/chat`

同步对话，请求体包含 `session_id` 和 `message`。

### `POST /api/chat/stream`

SSE 流式对话，返回 `task`、`step`、`done` 等事件。

## 工作流

### `POST /api/workflow/run`

运行多 Agent workflow，请求体包含 `task` 和 `workflow_type`。

## Trace

### `GET /api/traces/{task_id}`

按任务 ID 获取执行轨迹。

## Metrics

### `GET /api/metrics`

获取任务成功率、耗时等运行指标。

## Benchmark

### `POST /api/benchmark/run`

运行 Benchmark 用例集。

### `GET /api/benchmark/{run_id}`

获取 Benchmark Markdown 报告。

## CloudOps

### `GET /api/cloud-ops/modes`

获取云运维子模式。

### `POST /api/cloud-ops/run`

同步运行云运维任务。`mode` 支持 `k8s`、`resource`、`fault`、`cost`。

### `POST /api/cloud-ops/stream`

SSE 流式运行云运维任务。

### `GET /api/cloud-ops/knowledge`

检索运维知识库。

## 错误模型

API 错误统一返回 `code`、`message` 和可选 `details`，便于前端展示和面试讲解异常边界。