# 部署文档

## 本地部署

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
athena web --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000`。

## 环境变量

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `OPENAI_API_KEY` | LiteLLM 兼容模型 Key | `sk-...` |
| `ATHENA_LLM_MODEL` | 模型名称 | `deepseek/deepseek-chat` |
| `ATHENA_CONFIG` | 配置文件路径 | `config.yaml` |

## 生产化建议

1. 使用进程管理器或容器运行 `athena web`。
2. 将 `.env` 放入部署平台的 secret 管理，不提交仓库。
3. Web Console 前置 Nginx/Ingress，启用 HTTPS。
4. 接真实云账号前，先配置最小权限 AK/SK 和只读角色。
5. 高危操作必须保留人工确认与审计日志。

## Docker 化草案

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install -e .
CMD ["athena", "web", "--host", "0.0.0.0", "--port", "8000"]
```

## 健康检查

```powershell
curl http://127.0.0.1:8000/api/metrics
```

## 回滚策略

1. 保留上一版镜像或虚拟环境。
2. 配置变更和代码变更分开发布。
3. CloudOps 写操作默认关闭，仅在验证后开启 confirmed 流程。