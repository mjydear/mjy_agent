# Athena Agent — Kubernetes Deployment

## Overview
This document outlines the deployment process for the Athena Agent on Kubernetes, demonstrating a simple setup with examples.

### Example Deployment Steps

1. **Build and Push Docker Image**: Use the following commands to build and push the Docker image:
   ```bash
   docker build -t <registry>/athena-agent:latest .
   docker push <registry>/athena-agent:latest
   ```
2. **Deploy to Kubernetes**: Apply the Kubernetes manifests in order:
   ```bash
   kubectl apply -f k8s/configmap.yaml
   kubectl apply -f k8s/secret.yaml
   kubectl apply -f k8s/redis.yaml
   kubectl apply -f k8s/deployment.yaml
   kubectl apply -f k8s/hpa.yaml
   ```
3. **Check Status**: You can check the status of your pods and services using:
   ```bash
   kubectl -n athena get pods,svc,hpa
   ```

多副本 + HPA 弹性扩容 + 健康探针的生产部署清单。

## 部署顺序

```bash
# 1. 构建并推送镜像（替换成你的仓库）
docker build -t <registry>/athena-agent:latest .
docker push <registry>/athena-agent:latest
# 记得同步修改 deployment.yaml 的 image 字段

# 2. 按顺序应用
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml      # 生产用外部 Secret 管理器，勿提交真实值
kubectl apply -f k8s/redis.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/hpa.yaml

# 3. 查看状态
kubectl -n athena get pods,svc,hpa
```

## 关键设计

| 能力 | 清单 | 说明 |
|------|------|------|
| 多副本 | deployment.yaml `replicas: 2` | 无单点 |
| 弹性扩容 | hpa.yaml | CPU>70% 或 内存>80% 在 2~10 副本伸缩 |
| 滚动更新 | deployment.yaml `RollingUpdate maxUnavailable:0` | 发布不中断 |
| 存活/就绪探针 | deployment.yaml `livenessProbe/readinessProbe` | 假死自愈、未就绪不接流量 |
| 资源配额 | `resources.requests/limits` | 防止单 Pod 拖垮节点 |
| 缓存 | redis.yaml | LRU 淘汰，256MB 上限 |
| 密钥隔离 | secret.yaml | API Key / 模型密钥不进镜像 |

> HPA 基于资源指标需集群安装 metrics-server。基于 QPS/自定义指标扩容可接 Prometheus Adapter（见阶段4）。
