# Docker 引擎看门狗（U4/KTD7）
# 探活失败 → 恢复序列（停 Docker Desktop → wsl --shutdown → 重启）→ 轮询恢复
# 恢复互斥: in-progress 标记 15 分钟宽限期（覆盖 Docker Desktop 冷启动），防「杀掉-重启-再杀掉」死循环
# 失败计数在恢复序列开始前递增（任何中断路径都已计数）；连续 MAX_CONSECUTIVE_FAILURES 次后只告警（防风暴）
# 日志: C:\ProgramData\wecom-wddog\wddog.log（SYSTEM 与交互用户一致）
#
# 用法: powershell -ExecutionPolicy Bypass -File <注册目录>\wddog.ps1
# 注册计划任务: powershell -ExecutionPolicy Bypass -File scripts\wddog\register-task.ps1

param(
    [int]$ProbeTimeoutSeconds = 20,
    [int]$GraceMinutes = 15,
    [int]$MaxConsecutiveFailures = 3
)

$ErrorActionPreference = "SilentlyContinue"
$logDir = "C:\ProgramData\wecom-wddog"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir "wddog.log"
$markerFile = Join-Path $logDir "recover_in_progress.marker"
$failCounterFile = Join-Path $logDir "consecutive_failures.txt"
$dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

function Log($level, $msg) {
    $line = "{0} [{1}] {2}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $level, $msg
    Add-Content -Path $logFile -Value $line
    if ($level -in "ERROR", "WARN") { Write-Host $line }
}

# ---- 探活 ----
$probe = Start-Process -FilePath "docker" -ArgumentList "info" -NoNewWindow -PassThru -RedirectStandardOutput "$logDir\probe.out" -RedirectStandardError "$logDir\probe.err"
if (-not $probe.WaitForExit($ProbeTimeoutSeconds * 1000)) {
    try { $probe.Kill() } catch {}
    $healthy = $false
    Log WARN "探活超时（>${ProbeTimeoutSeconds}s）"
} else {
    $healthy = ($probe.ExitCode -eq 0)
}

if ($healthy) {
    Log INFO "heartbeat ok"
    if (Test-Path $failCounterFile) { Remove-Item $failCounterFile -Force }
    if (Test-Path $markerFile) { Remove-Item $markerFile -Force }
    exit 0
}

# ---- 引擎不健康：恢复互斥检查 ----
if (Test-Path $markerFile) {
    $markerAge = (Get-Date) - (Get-Item $markerFile).LastWriteTime
    if ($markerAge.TotalMinutes -lt $GraceMinutes) {
        Log INFO "恢复进行中（标记 {0:N0} 分钟前，宽限 {1} 分钟），本轮跳过" -f $markerAge.TotalMinutes, $GraceMinutes
        exit 0
    }
    Log WARN "恢复标记已超宽限期（$($markerAge.TotalMinutes.ToString('N0')) 分钟），重新恢复"
}

# ---- 连续失败熔断（防风暴）——计数在动手前先递增，任何中断路径都已计入 ----
$failCount = 0
if (Test-Path $failCounterFile) { $failCount = [int](Get-Content $failCounterFile -Raw) }
if ($failCount -ge $MaxConsecutiveFailures) {
    Log ERROR "已连续 $failCount 次恢复失败，停止自动恢复 — 请人工介入（查看 Docker Desktop / WSL）"
    exit 1
}
Set-Content -Path $failCounterFile -Value ($failCount + 1)

# ---- 恢复序列（本会话已验证） ----
Log WARN "引擎不健康，开始恢复（第 $($failCount + 1) 次）"
New-Item -ItemType File -Path $markerFile -Force | Out-Null

Stop-Process -Name "Docker Desktop", "com.docker.backend", "com.docker.build", "com.docker.service" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 5
wsl --shutdown
Start-Sleep -Seconds 5
Start-Process $dockerExe

# ---- 轮询恢复（12 次 x 最长 40s = 8 分钟，确保在计划任务 ExecutionTimeLimit 20 分钟内完成） ----
$restored = $false
foreach ($i in 1..12) {
    Start-Sleep -Seconds 10
    $p = Start-Process -FilePath "docker" -ArgumentList "info" -NoNewWindow -PassThru -RedirectStandardOutput "$logDir\probe.out" -RedirectStandardError "$logDir\probe.err"
    if ($p.WaitForExit(30000) -and $p.ExitCode -eq 0) { $restored = $true; break }
}

Remove-Item $markerFile -Force -ErrorAction SilentlyContinue

if ($restored) {
    Log INFO "引擎已恢复"
    if (Test-Path $failCounterFile) { Remove-Item $failCounterFile -Force }
    exit 0
}

Log ERROR "恢复轮询超时（约 8 分钟），失败计数已累加；下一周期若仍失败将继续计满后转人工"
exit 1
