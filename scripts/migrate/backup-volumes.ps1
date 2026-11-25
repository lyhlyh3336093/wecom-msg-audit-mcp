# 数据卷备份 v2 — 先探测真实卷名再备份，产物大小校验（防空 tar 假成功）
# 用法: powershell -ExecutionPolicy Bypass -File scripts\migrate\backup-volumes.ps1 [-OutputDir <目录>]
# 内置恢复循环: 引擎不可用时自动执行恢复序列（最多 3 轮）

param([string]$OutputDir = "D:\volumes-backup")

# Continue: docker CLI 的 stderr 在 PS5 Stop 模式下会被误判为终止错误，探测循环需容忍
$ErrorActionPreference = "Continue"
function Step($msg) { Write-Host "[backup] $msg" -ForegroundColor Cyan }

Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

# ---- 恢复循环 ----
$ready = $false
for ($round = 1; $round -le 3; $round++) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Write-Host "[backup] 引擎不可用（第 $round 轮恢复）..." -ForegroundColor Yellow
    wsl --shutdown
    Start-Sleep -Seconds 8
    Stop-Process -Name "Docker Desktop", "com.docker.backend", "com.docker.build", "com.docker.service" -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    foreach ($i in 1..40) {
        Start-Sleep -Seconds 6
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    }
    if ($ready) { break }
}
if (-not $ready) { Write-Host "[backup] 引擎 3 轮恢复失败 — 请人工介入" -ForegroundColor Red; exit 1 }
Step "引擎就绪"

# ---- 探测真实卷名 ----
Step "docker volume ls 探测真实卷名"
docker volume ls --format "{{.Name}}" | Out-File (Join-Path $OutputDir "volume_list.txt") -Encoding utf8
$allVolumes = Get-Content (Join-Path $OutputDir "volume_list.txt")
Write-Host ($allVolumes | ForEach-Object { "  卷: $_" })

$pgVol = $allVolumes | Where-Object { $_ -match "pg" } | Select-Object -First 1
$minioVol = $allVolumes | Where-Object { $_ -match "minio" } | Select-Object -First 1
$bgeM3Vol = $allVolumes | Where-Object { $_ -match "bge_m3" } | Select-Object -First 1
$bgeRerankerVol = $allVolumes | Where-Object { $_ -match "bge_reranker" } | Select-Object -First 1
if (-not $pgVol) { Write-Host "[backup] 未找到 pg 卷！" -ForegroundColor Red; docker volume ls; exit 1 }

$pgTag = "docker.m.daocloud.io/pgvector/pgvector:pg17"
$jobs = @(
    @{ vol = $pgVol; out = "pg_data.tar.gz"; image = $pgTag },
    @{ vol = $minioVol; out = "minio_data.tar.gz"; image = $pgTag },
    @{ vol = $bgeM3Vol; out = "bge_m3_data.tar.gz"; image = $pgTag },
    @{ vol = $bgeRerankerVol; out = "bge_reranker_data.tar.gz"; image = $pgTag }
) | Where-Object { $_.vol }

$stamp = (Get-Date).ToString("o")
$failed = @()
foreach ($job in $jobs) {
    Step "备份 $($job.vol) -> $($job.out)"
    $outFile = Join-Path $OutputDir $job.out
    if (Test-Path $outFile) { Remove-Item $outFile -Force }
    docker volume inspect $job.vol *> $null
    if ($LASTEXITCODE -ne 0) { Write-Warning "卷 $($job.vol) 不存在，跳过"; continue }
    docker run --rm -v "$($job.vol):/data" -v "${OutputDir}:/dump" --entrypoint sh `
        $pgTag -c "cd /data && tar czf /dump/$($job.out) ."
    if ($LASTEXITCODE -ne 0) { $failed += $job.vol; continue }
    $size = (Get-Item $outFile).Length
    if ($size -lt 200) { Write-Warning "产物仅 $size 字节（可能空卷）— $($job.vol)" } else { Write-Host "  ✓ $($job.out) = $([math]::Round($size/1MB,1)) MB" -ForegroundColor Green }
}
if ($failed.Count -gt 0) { Write-Host "[backup] 以下卷备份失败: $($failed -join ', ')" -ForegroundColor Red }

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Get-ChildItem $OutputDir -File | Format-Table Name, Length -AutoSize
Write-Host "备份目录: $OutputDir（宿主机磁盘，不受 Docker 重置影响）" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
