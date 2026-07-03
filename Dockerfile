# syntax=docker/dockerfile:1
# 多阶段构建：builder 装依赖，runtime 只带运行所需，镜像更小、攻击面更低。

FROM python:3.12-slim AS builder
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ATHENA_WEB_HOST=0.0.0.0 \
    ATHENA_WEB_PORT=8000
# 非 root 运行，符合最小权限原则
RUN groupadd -r athena && useradd -r -g athena athena
COPY --from=builder /opt/venv /opt/venv
COPY . .
RUN chown -R athena:athena /app
USER athena
EXPOSE 8000
# 健康检查：命中 metrics 端点即视为存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/metrics',timeout=3).status==200 else 1)"
# 生产用多 worker；uvicorn 直接加载 ASGI app 对象
CMD ["uvicorn", "athena.api.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
