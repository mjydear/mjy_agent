<#
.SYNOPSIS
    向本地 Athena 发送一条 mock Alertmanager 告警，触发自动诊断闭环（Windows PowerShell）。

.DESCRIPTION
    前置：Athena Web 服务已启动（默认 http://localhost:8000）。
    使用：pwsh -File deploy/kind-demo/send-alert.ps1 [-BaseUrl http://localhost:8000]
#>
param(
    [string]$BaseUrl = "http://localhost:8000"
)
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$payloadPath = Join-Path $ScriptDir "alertmanager-webhook-example.json"
$payload = Get-Content -Raw -Path $payloadPath

Write-Host "POST $BaseUrl/api/alerts/webhook"
$response = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/alerts/webhook" `
    -ContentType "application/json" -Body $payload
$response | ConvertTo-Json -Depth 8

Write-Host ""
Write-Host "查看告警历史：Invoke-RestMethod $BaseUrl/api/alerts/history"
