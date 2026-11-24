# 迁移导入脚本（U3）— 在新机器执行，从加密迁移包恢复全栈
# 用法:  powershell -ExecutionPolicy Bypass -File scripts\migrate\import.ps1 -Pack <migration_pack.7z> [-ProjectDir <目标目录>] [-Rehearsal] [-Force]
#
# 前置（见 MIGRATION.md）: Docker Desktop 已安装并运行；7z 已安装；已解压获得密码（独立渠道）
# 步骤: 解压 → manifest 校验 → docker load → 恢复数据卷 → 启动 → health + 容器内 RAG 连通检查

param(
    [Parameter(Mandatory = $true)][string]$Pack,
    [string]$ProjectDir = ".\wecom-restored",
    [switch]$Rehearsal,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "[import] $msg" -ForegroundColor Cyan }
function Die($msg) { Write-Host "[import] $msg" -ForegroundColor Red; exit 1 }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot

if (-not (Test-Path $Pack)) { Die "迁移包不存在: $Pack" }
if (Test-Path $ProjectDir) {
    if (-not $Force) { Die "目标目录已存在: $ProjectDir — 用 -Force 覆盖"; }
    Remove-Item $ProjectDir -Recurse -Force
}

# ---- 前置检查 ----
Step "前置检查: Docker 引擎"
docker info *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker 引擎不可用 — 先启动 Docker Desktop（僵死时按 MIGRATION.md 恢复序列）" }
$drive = (Get-PSDrive ($ProjectDir.Substring(0,1))).Free
if ($drive -lt 30GB) { Write-Warning "目标盘剩余空间不足 30GB（镜像+模型卷约需 20-25GB），继续但可能中途失败" }

# ---- 解压（提示输入密码，不落盘） ----
Step "解压加密归档（密码不入脚本/日志）"
New-Item -ItemType Directory -Path $ProjectDir | Out-Null
$securePwd = Read-Host "输入迁移包解压密码" -AsSecureString
$plainPwd = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePwd))
7z x "-p$plainPwd" -o"$ProjectDir\pack" $Pack | Out-Null
if ($LASTEXITCODE -ne 0) { Die "解压失败（密码错误或归档损坏）" }
$pack = Join-Path $ProjectDir "pack"

# ---- manifest 校验 ----
Step "按 manifest.json 逐文件校验 SHA256"
$manifest = Get-Content (Join-Path $pack "manifest.json") -Raw | ConvertFrom-Json
$bad = @()
foreach ($entry in $manifest.files) {
    $f = Join-Path $pack $entry.file
    if (-not (Test-Path $f)) { $bad += "$($entry.file) 缺失"; continue }
    $h = (Get-FileHash $f -Algorithm SHA256).Hash.ToLower()
    if ($h -ne $entry.sha256) { $bad += "$($entry.file) 校验和不匹配" }
}
if ($bad.Count -gt 0) { $bad | ForEach-Object { Write-Host "  ✗ $_" -ForegroundColor Red }; Die "manifest 校验不过，中止" }
Write-Host "  ✓ $($manifest.files.Count) 个文件校验通过"

# ---- docker load ----
Step "docker load 全部镜像"
Get-ChildItem $pack -Filter "img_*.tar" | ForEach-Object {
    docker load -i $_.FullName
    if ($LASTEXITCODE -ne 0) { Die "docker load 失败: $($_.Name)" }
}

# ---- 布局项目目录 ----
Step "布置 compose / env / sdk / secrets"
Copy-Item (Join-Path $pack "docker-compose.yml") $ProjectDir -Force
Copy-Item (Join-Path $pack "wecom.env") (Join-Path $ProjectDir ".env") -Force
foreach ($zip in @("sdk", "secrets")) {
    if (Test-Path (Join-Path $pack "$zip.zip")) {
        New-Item -ItemType Directory -Path (Join-Path $ProjectDir $zip) | Out-Null
        Expand-Archive (Join-Path $pack "$zip.zip") (Join-Path $ProjectDir $zip) -Force
    }
}

# ---- KTD8: 容器侧 RAG_API_URL 转换 ----
Step "KTD8: 容器内 RAG_API_URL 改为 host.docker.internal"
$envPath = Join-Path $ProjectDir ".env"
(Get-Content $envPath -Raw) -replace "RAG_API_URL=http://127\.0\.0\.1:8001", "RAG_API_URL=http://host.docker.internal:8001" |
    Set-Content $envPath -Encoding UTF8 -NoNewline

# ---- aihub 恢复 ----
$aihubDir = Join-Path $ProjectDir "aihub"
New-Item -ItemType Directory -Path (Join-Path $aihubDir "docker") | Out-Null
if (Test-Path (Join-Path $pack "aihub-docker\Dockerfile")) {
    Copy-Item (Join-Path $pack "aihub-docker\*") (Join-Path $aihubDir "docker") -Recurse -Force
}
Copy-Item (Join-Path $pack "aihub.env") (Join-Path $aihubDir ".env") -Force
Copy-Item (Join-Path $pack "aihub-docker-compose.yml") (Join-Path $aihubDir "docker\docker-compose.yml") -Force -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $aihubDir "docker\docker-compose.yml"))) {
    Write-Warning "迁移包内缺 aihub compose 文件，将从本仓库 scripts/migrate 附带副本恢复"
}

Step "恢复 Postgres / MinIO / BGE 数据卷"
$pgTag = "docker.m.daocloud.io/pgvector/pgvector:pg17"
if (Test-Path (Join-Path $pack "aihub_knowledge.dump")) {
    docker volume create aihub-knowledge_pg_data | Out-Null
    docker run --rm --name aihub-mig-restore -v aihub-knowledge_pg_data:/var/lib/postgresql/data `
        -v "${pack}:/dump" $pgTag bash -c "pg_restore -U aihub -d aihub_knowledge --no-owner /dump/aihub_knowledge.dump --exit-on-error" 2>&1 | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) { Die "pg_restore 失败 — 现场已保留（卷 aihub-knowledge_pg_data），排查后重跑本脚本" }
}
foreach ($vol in @(@("minio_data.tar", "aihub-knowledge_minio_data", "docker.m.daocloud.io/minio/minio:latest"),
                   @("bge_m3_data.tar", "aihub-knowledge_bge_m3_data", $pgTag),
                   @("bge_reranker_data.tar", "aihub-knowledge_bge_reranker_data", $pgTag))) {
    $tar = Join-Path $pack $vol[0]
    if (Test-Path $tar) {
        docker volume create $vol[1] | Out-Null
        docker run --rm --name "aihub-mig-load-$($vol[1])" -v "$($vol[1]):/data" -v "${pack}:/dump" --entrypoint sh `
            $vol[2] -c "cd /dump && tar xf $($vol[0]) -C /data"
        if ($LASTEXITCODE -ne 0) { Die "卷恢复失败: $($vol[1])" }
    } else { Write-Warning "缺 $($vol[0])，跳过该卷" }
}

# ---- 启动 ----
$composeArgs = @("-f", "docker\docker-compose.yml")
if ($Rehearsal) {
    Step "-Rehearsal: 生成 override（剥离 container_name、重映射端口）"
    $override = Join-Path $ProjectDir "rehearsal-override.yml"
    @"
services:
  wecom-mcp:
    container_name: ""
    ports: !override ["127.0.0.1:18331:8331"]
  app:
    container_name: ""
    ports: !override ["127.0.0.1:18001:8001"]
  postgres:
    container_name: ""
    ports: !override ["15482:5432"]
  redis:
    container_name: ""
    ports: !override ["16382:6379"]
  minio:
    container_name: ""
    ports: !override ["19082:9000", "19083:9001"]
  bge-m3:
    container_name: ""
    ports: !override ["18082:80"]
  bge-reranker:
    container_name: ""
    ports: !override ["18083:80"]
"@ | Set-Content $override -Encoding UTF8
    $composeArgs += @("-f", "rehearsal-override.yml")
}

Step "启动 aihub 全栈"
Push-Location $aihubDir
docker compose -f docker/docker-compose.yml up -d
if ($LASTEXITCODE -ne 0) { Pop-Location; Die "aihub compose up 失败" }
Pop-Location

Step "启动本项目"
docker compose @composeArgs up -d
if ($LASTEXITCODE -ne 0) { Die "本项目 compose up 失败" }

# ---- 验证 ----
Step "health 探活（等待最多 120 秒）"
$port = if ($Rehearsal) { 18331 } else { 8331 }
$ok = $false
foreach ($i in 1..24) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
}
if (-not $ok) { Die "本项目 /health 未就绪 — 查 docker compose logs" }
Write-Host "  ✓ 本项目 health 200"

Step "容器内 RAG 连通性检查（KTD8）"
$ragPort = if ($Rehearsal) { 18001 } else { 8001 }
try {
    $key = (Select-String -Path $envPath -Pattern "^RAG_API_TOKEN=(.*)$").Matches[0].Groups[1].Value.Trim()
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$ragPort/api/health" -Headers @{ "X-API-Key" = $key } -UseBasicParsing -TimeoutSec 10
    Write-Host "  ✓ RAG /api/health HTTP $($r.StatusCode)"
} catch { Die "RAG 连通性检查失败: $($_.Exception.Message) — 检查 aihub 容器日志与 KTD8 配置" }

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "恢复完成。剩余人工验证单（MIGRATION.md）:"
Write-Host "  [ ] 拉取消息: POST http://127.0.0.1:$port/api/pull"
Write-Host "  [ ] 群问答:   群内 @智能小助手 测试（企微渠道）"
Write-Host "  [ ] 仪表盘:   http://127.0.0.1:$port/"
Write-Host "  [ ] wxbridge: 按部署文档启动（若已配置）"
if ($Rehearsal) { Write-Host "  [ ] 演练完成: docker compose -p 演练项目 down 并清理" }
Write-Host "  [ ] 退役清机: 按 MIGRATION.md 退役章节执行（勿跳过）"
Write-Host "========================================" -ForegroundColor Green
