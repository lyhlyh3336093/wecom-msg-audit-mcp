# 注册 Docker 引擎看门狗计划任务（U4）
# 每 5 分钟运行一次 wddog.ps1；需要管理员权限注册 SYSTEM 任务
# 用法: powershell -ExecutionPolicy Bypass -File scripts\wddog\register-task.ps1
# 卸载: Unregister-ScheduledTask -TaskName "WeComDockerWatchdog" -Confirm:$false

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "wddog.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName "WeComDockerWatchdog" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "计划任务 WeComDockerWatchdog 已注册（每 5 分钟，SYSTEM 权限）" -ForegroundColor Green
Write-Host "日志: $env:LOCALAPPDATA\wecom-wddog\wddog.log"
Write-Host "立即试跑: Start-ScheduledTask -TaskName WeComDockerWatchdog; Get-Content `"$env:LOCALAPPDATA\wecom-wddog\wddog.log`" -Tail 5"
