#!/usr/bin/env bash
# 向本地 Athena 发送一条 mock Alertmanager 告警，触发自动诊断闭环。
#
# 前置：Athena Web 服务已启动（默认 http://localhost:8000）。
# 使用：bash deploy/kind-demo/send-alert.sh [ATHENA_BASE_URL]
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "POST ${BASE_URL}/api/alerts/webhook"
curl -sS -X POST "${BASE_URL}/api/alerts/webhook" \
  -H "Content-Type: application/json" \
  --data @"${SCRIPT_DIR}/alertmanager-webhook-example.json"
echo ""
echo "查看告警历史：curl -sS ${BASE_URL}/api/alerts/history"
