# 快速开始

## 环境要求

- Windows、macOS 或 Linux
- Python 3.11+
- PowerShell、Bash 或兼容终端

## 安装

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

## 验证安装

```powershell
python -m pytest
python examples/demo1_code_analysis.py
python examples/demo4_k8s_diagnose.py
```

## 配置模型

本地 mock demo 不需要模型 Key。真实对话需要配置 `.env`：

```env
OPENAI_API_KEY=你的 API Key
ATHENA_LLM_MODEL=deepseek/deepseek-chat
```

## CLI 使用

```powershell
athena chat "请解释这个项目的核心架构"
```

## Web Console

```powershell
athena web --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000`。

## 6 个面试 Demo

```powershell
python examples/demo1_code_analysis.py
python examples/demo2_self_evolution.py
python examples/demo3_debugger.py
python examples/demo4_k8s_diagnose.py
python examples/demo5_cost_optimize.py
python examples/demo6_alert_auto_handle.py
```