# 开发指南

## 本地开发

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

## 代码结构

- `athena/agent`：Agent 协议、ReAct 执行器、多 Agent workflow。
- `athena/tools`：工具注册、执行、安全权限、内置工具。
- `athena/memory`：Working、Profile、Long-term、Skill 和治理模块。
- `athena/learning`：GEPA 复杂度评估、轨迹记录、Skill 生成与优化。
- `athena/api`：FastAPI schema、service 和 routes。
- `examples`：可运行演示脚本。
- `docs`：功能说明、demo、benchmark 和面试材料。

## 常用检查

```powershell
black .
isort .
mypy athena examples tests
python -m pytest
```

如果工具未安装，可执行：

```powershell
pip install black isort mypy pytest-cov
```

## 新增工具规范

1. 在 `athena/tools/builtin` 下实现函数。
2. 使用 `ToolRegistry.register` 注册。
3. 参数必须有清晰类型注解。
4. 工具失败优先返回可解释错误，不吞异常。
5. 涉及文件、命令、云操作时必须经过路径、权限或人工确认边界。

## 新增 Demo 规范

1. 默认不依赖真实 LLM Key 或真实云账号。
2. 输出要适合面试录屏，包含标题、步骤和结果。
3. 尽量复用服务层或工具层真实入口，避免只写孤立 mock。
4. README 和 `docs/demos/demo_guide.md` 同步更新。

## FAQ

### 为什么核心不依赖第三方 Agent 框架？

为了让执行循环、工具协议、记忆压缩和自进化机制都可解释、可测试，也方便面试时讲清每个关键设计。

### 为什么 demo 默认使用 mock？

面试环境经常没有云账号、集群或稳定网络。mock-first 可以保证演示稳定，真实接入通过同一接口替换 client。

### 如何接真实 K8s？

从 `K8sOpsTools` 的 client 注入边界开始替换，把 mock client 换成读取 kubeconfig 的 Kubernetes client。