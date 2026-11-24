# 注册 Docker 引擎看门狗计划任务（U4）
# 每 5 分钟运行一次；脚本复制到仅管理员可写的 ProgramData 目录，避免 SYSTEM 执行用户可写文件（本地提权面）
# 需要管理员权限注册
# 用法: powershell -ExecutionPolicy Bypass -File scripts\wddog\register-task.ps1
# 卸载: Unregister-ScheduledTask -TaskName "WeComDockerWatchdog" -Confirm:$false

$ErrorActionPreference = "Stop"
$sourceScript = Join-Path $PSScriptRoot "wddog.ps1"
$deployDir = "C:\ProgramData\wecom-wddog"
$deployedScript = Join-Path $deployDir "wddog.ps1"

New-Item -ItemType Directory -Path $deployDir -Force | Out-Null
Copy-Item $sourceScript $deployedScript -Force
# 收紧 ACL：仅 Administrators 与 SYSTEM 可写
icacls $deployedScript /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" /grant "Users:R" | Out-Null

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$deployedScript`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName "WeComDockerWatchdog" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "计划任务 WeComDockerWatchdog 已注册（每 5 分钟，SYSTEM，脚本副本: $deployedScript）" -ForegroundColor Green
Write-Host "日志: C:\ProgramData\wecom-wddog\wddog.log"
Write-Host "立即试跑: Start-ScheduledTask -TaskName WeComDockerWatchdog; Get-Content 'C:\ProgramData\wecom-wddog\wddog.log' -Tail 5"
