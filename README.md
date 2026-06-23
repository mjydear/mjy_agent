# Athena Agent

Athena Agent 是一个从零实现的自进化企业级开发者智能助手 MVP。第一阶段重点不是封装现成 Agent 框架，而是跑通一条可解释、可测试、可扩展的 Agent 基础闭环：LLM 接入、Prompt 组装、装饰器式工具注册、ReAct 执行循环、短期记忆、CLI 入口和向量存储抽象。

## 第一阶段范围

- Python 3.11+ 标准包结构。
- 基于 LiteLLM 的 LLM 接入层，提供与具体模型厂商解耦的统一接口。
- Prompt 上下文组装：系统提示、短期记忆、工具描述、执行草稿和用户输入。
- 装饰器模式的工具注册中心与工具调用机制。
- ReAct 执行循环，支持 Thought、Action、Observation 和最终答案处理。
- Token 感知的短期工作记忆，并提供 MVP 级别的裁剪策略。
- 基于 Typer 的 CLI 命令行入口。
- VectorStore 协议，包含内存 fallback 和 Milvus 适配边界。

## 快速开始

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
athena chat "你好，请介绍一下 Athena Agent"
```

根据你选择的 LiteLLM 模型提供方配置 API Key。当前默认模型配置在 `config.yaml` 中，也可以通过环境变量 `ATHENA_LLM_MODEL` 覆盖。

如果使用 DeepSeek，可以在项目根目录创建 `.env` 文件：

```env
OPENAI_API_KEY=你的 DeepSeek API Key
```

项目会在 DeepSeek 模型下自动把 `OPENAI_API_KEY` 兼容映射为 `DEEPSEEK_API_KEY`。

## 开发检查

```powershell
pytest
```

## 设计说明

Athena 的核心逻辑刻意不引入 LangChain 和 LlamaIndex。Agent 执行循环、Prompt 组装、工具注册、记忆系统和向量存储接口都由项目直接实现，目的是让每个关键环节都能在面试中讲清楚原理，也能在后续阶段继续扩展为更完整的企业级 Agent 架构。
