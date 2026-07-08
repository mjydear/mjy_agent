#!/usr/bin/env bash
# 启动本地 kind 演示集群并部署异常工作负载 + Prometheus。
#
# 前置依赖：docker、kind、kubectl。
# 使用：bash deploy/kind-demo/start-kind.sh
set -euo pipefail

CLUSTER_NAME="athena-demo"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/4] 创建 kind 集群 ${CLUSTER_NAME} ..."
if kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  echo "     集群已存在，跳过创建。"
else
  kind create cluster --name "${CLUSTER_NAME}" --config "${SCRIPT_DIR}/kind-cluster.yaml"
fi

echo "[2/4] 创建演示命名空间 athena-demo ..."
kubectl create namespace athena-demo --dry-run=client -o yaml | kubectl apply -f -

echo "[3/4] 部署异常工作负载样例 ..."
kubectl apply -n athena-demo -f "${SCRIPT_DIR}/workloads/"

echo "[4/4] 部署本地 Prometheus ..."
kubectl apply -f "${SCRIPT_DIR}/prometheus.yaml"

echo ""
echo "完成。常用检查命令："
echo "  kubectl get pods -n athena-demo"
echo "  kubectl get events -n athena-demo --sort-by=.lastTimestamp"
echo "  Prometheus: http://localhost:30090"
echo ""
echo "接下来用 real 模式启动 Athena（见 deploy/kind-demo/README.md）。"
