# wxbridge 常驻启动包装（供计划任务调用）
# 用法: powershell -ExecutionPolicy Bypass -File scripts\wxbridge_start.ps1 [-RepoRoot <路径>]
# 日志: <RepoRoot>\data\wxbridge.log（轮转：>10MB 归档为 .1）

param([string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))

$ErrorActionPreference = "Continue"
Set-Location $RepoRoot

$logDir = Join-Path $RepoRoot "data"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir "wxbridge.log"
if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 10MB)) {
    Move-Item $logFile "$logFile.1" -Force
}

# uv 在 SYSTEM/计划任务环境可能不在 PATH，优先完整路径
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { $uv = "$env:LOCALAPPDATA\Programs\uv\uv.exe" }

while ($true) {
    "---- wxbridge start $((Get-Date).ToString('o')) ----" | Add-Content $logFile
    & $uv run python -m wecom_bot.wxbridge *>> $logFile
    $code = $LASTEXITCODE
    "---- wxbridge exit $code $((Get-Date).ToString('o')) ----" | Add-Content $logFile
    if ($code -eq 0) { break }
    Start-Sleep -Seconds 15  # 崩溃自动重启（15s 退避）
}
