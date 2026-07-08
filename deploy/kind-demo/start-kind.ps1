<#
.SYNOPSIS
    启动本地 kind 演示集群并部署异常工作负载 + Prometheus（Windows PowerShell）。

.DESCRIPTION
    前置依赖：docker、kind、kubectl 均已在 PATH 中。
    使用：pwsh -File deploy/kind-demo/start-kind.ps1
#>
$ErrorActionPreference = "Stop"
$ClusterName = "athena-demo"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "[1/4] 创建 kind 集群 $ClusterName ..."
$existing = (kind get clusters) 2>$null
if ($existing -contains $ClusterName) {
    Write-Host "     集群已存在，跳过创建。"
} else {
    kind create cluster --name $ClusterName --config (Join-Path $ScriptDir "kind-cluster.yaml")
}

Write-Host "[2/4] 创建演示命名空间 athena-demo ..."
kubectl create namespace athena-demo --dry-run=client -o yaml | kubectl apply -f -

Write-Host "[3/4] 部署异常工作负载样例 ..."
kubectl apply -n athena-demo -f (Join-Path $ScriptDir "workloads")

Write-Host "[4/4] 部署本地 Prometheus ..."
kubectl apply -f (Join-Path $ScriptDir "prometheus.yaml")

Write-Host ""
Write-Host "完成。常用检查命令："
Write-Host "  kubectl get pods -n athena-demo"
Write-Host "  kubectl get events -n athena-demo --sort-by=.lastTimestamp"
Write-Host "  Prometheus: http://localhost:30090"
Write-Host ""
Write-Host "接下来用 real 模式启动 Athena（见 deploy/kind-demo/README.md）。"
