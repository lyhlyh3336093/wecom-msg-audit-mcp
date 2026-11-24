# 迁移导入脚本（U3）— 在新机器执行，从加密迁移包恢复全栈
# 用法:  powershell -ExecutionPolicy Bypass -File scripts\migrate\import.ps1 -Pack <migration_pack.7z> [-ProjectDir <绝对目录>] [-Rehearsal] [-Force]
#
# 前置（见 MIGRATION.md）: Docker Desktop 已运行；7z 已安装；解压密码经独立渠道获得
# 步骤: 解压（密码走临时响应文件，不进命令行/日志）→ manifest 校验 → docker load → 卷恢复（幂等：先删旧卷）→
#       KTD8 容器侧 RAG_API_URL 转换（带命中断言）→ 启动 → health + 容器内 RAG 连通检查

param(
    [Parameter(Mandatory = $true)][string]$Pack,
    [string]$ProjectDir = "",
    [switch]$Rehearsal,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "[import] $msg" -ForegroundColor Cyan }
function Die($msg) { Write-Host "[import] $msg" -ForegroundColor Red; exit 1 }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot

if (-not (Test-Path $Pack)) { Die "迁移包不存在: $Pack" }
if ([string]::IsNullOrWhiteSpace($ProjectDir)) {
    $ProjectDir = Join-Path (Get-Location).Path "wecom-restored"
}
if (-not [System.IO.Path]::IsPathRooted($ProjectDir)) {
    $ProjectDir = Join-Path (Get-Location).Path $ProjectDir
}
$driveLetter = $null
try { $driveLetter = [System.IO.Path]::GetPathRoot((Resolve-Path (Split-Path -Parent $ProjectDir) -ErrorAction Stop).Path).Substring(0, 1) } catch {}
if ($driveLetter -and (Test-Path "$($driveLetter):\")) {
    $free = (Get-PSDrive $driveLetter).Free
    if ($free -lt 30GB) { Write-Warning "目标盘剩余空间不足 30GB（镜像+模型卷约需 20-25GB），继续但可能中途失败" }
} else {
    Write-Warning "磁盘空间检查跳过（路径暂不存在或无法解析盘符）"
}

if (Test-Path $ProjectDir) {
    if (-not $Force) { Die "目标目录已存在: $ProjectDir — 用 -Force 覆盖"; }
    Remove-Item $ProjectDir -Recurse -Force
}

# ---- 前置检查 ----
Step "前置检查: Docker 引擎"
docker info *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker 引擎不可用 — 先启动 Docker Desktop（僵死时按 MIGRATION.md 恢复序列）" }

# ---- 解压（密码走临时响应文件，不出现在命令行/日志） ----
Step "解压加密归档（密码经临时响应文件传递）"
New-Item -ItemType Directory -Path $ProjectDir | Out-Null
$securePwd = Read-Host "输入迁移包解压密码" -AsSecureString
$plainPwd = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePwd))
$responseFile = Join-Path $env:TEMP ("mig_" + [guid]::NewGuid().ToString("N") + ".txt")
Set-Content -Path $responseFile -Value "-p$plainPwd" -Encoding ASCII -NoNewline
try {
    & 7z x "@$responseFile" -o"$ProjectDir\pack" $Pack | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "解压失败（密码错误或归档损坏）" }
} finally {
    Remove-Item $responseFile -Force -ErrorAction SilentlyContinue
}
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
foreach ($z in @("sdk", "secrets")) {
    if (Test-Path (Join-Path $pack "$z.7z")) {
        New-Item -ItemType Directory -Path (Join-Path $ProjectDir $z) | Out-Null
        & 7z x -y "-o$(Join-Path $ProjectDir $z)" (Join-Path $pack "$z.7z") | Out-Null
        if ($LASTEXITCODE -ne 0) { Die "$z 恢复失败" }
    }
}

# ---- KTD8: 容器侧 RAG_API_URL 转换（带命中断言） ----
Step "KTD8: 容器内 RAG_API_URL 改为 host.docker.internal"
$envPath = Join-Path $ProjectDir ".env"
$envRaw = Get-Content $envPath -Raw
$converted = $envRaw -replace "RAG_API_URL=http://(127\.0\.0\.1|localhost):8001", "RAG_API_URL=http://host.docker.internal:8001"
if ($converted -eq $envRaw) {
    if (-not $Force) { Die "RAG_API_URL 未找到可替换的 127.0.0.1/localhost:8001 形式 — 请人工检查 .env 后用 -Force 继续" }
    Write-Warning "RAG_API_URL 替换未命中（-Force 继续）— 迁移后人工核对容器内 RAG 连通"
} else {
    Write-Host "  ✓ RAG_API_URL 已转换为 host.docker.internal"
}
Set-Content $envPath -Value $converted -Encoding UTF8 -NoNewline

# ---- aihub 恢复 ----
$aihubDir = Join-Path $ProjectDir "aihub"
New-Item -ItemType Directory -Path (Join-Path $aihubDir "docker") | Out-Null
if (Test-Path (Join-Path $pack "aihub-docker\Dockerfile")) {
    Copy-Item (Join-Path $pack "aihub-docker\*") (Join-Path $aihubDir "docker") -Recurse -Force
}
Copy-Item (Join-Path $pack "aihub.env") (Join-Path $aihubDir ".env") -Force
if (-not (Test-Path (Join-Path $pack "aihub-docker-compose.yml"))) { Die "迁移包缺 aihub-docker-compose.yml（export 产物不完整）" }
Copy-Item (Join-Path $pack "aihub-docker-compose.yml") (Join-Path $aihubDir "docker\docker-compose.yml") -Force

Step "恢复 Postgres / MinIO / BGE 数据卷（幂等：先删旧卷）"
foreach ($vol in @("aihub-knowledge_pg_data", "aihub-knowledge_minio_data",
                   "aihub-knowledge_bge_m3_data", "aihub-knowledge_bge_reranker_data")) {
    docker volume rm $vol -f 2>&1 | Out-Null
    docker volume create $vol | Out-Null
}
$pgTag = "docker.m.daocloud.io/pgvector/pgvector:pg17"
if (Test-Path (Join-Path $pack "aihub_knowledge.dump")) {
    # pg_restore 需要运行中的 Postgres：先用临时容器起库，restore 后由正式 compose 接管同一卷
    docker run -d --name aihub-mig-pg -v aihub-knowledge_pg_data:/var/lib/postgresql/data `
        -e POSTGRES_DB=aihub_knowledge -e POSTGRES_USER=aihub -e POSTGRES_PASSWORD=aihub_dev_2026 $pgTag | Out-Null
    $pgReady = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        docker exec aihub-mig-pg pg_isready -U aihub -d aihub_knowledge *> $null
        if ($LASTEXITCODE -eq 0) { $pgReady = $true; break }
    }
    if (-not $pgReady) { Die "临时 Postgres 未就绪" }
    docker exec aihub-mig-pg pg_restore -U aihub -d aihub_knowledge --no-owner /hostdump/aihub_knowledge.dump --exit-on-error 2>&1 | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) { Die "pg_restore 失败 — 现场已保留（容器 aihub-mig-pg、卷 aihub-knowledge_pg_data），排查后 -Force 重跑" }
    docker rm -f aihub-mig-pg | Out-Null
}
foreach ($pair in @(@("minio_data.tar", "aihub-knowledge_minio_data", "docker.m.daocloud.io/minio/minio:latest"),
                   @("bge_m3_data.tar", "aihub-knowledge_bge_m3_data", $pgTag),
                   @("bge_reranker_data.tar", "aihub-knowledge_bge_reranker_data", $pgTag))) {
    $tar = Join-Path $pack $pair[0]
    if (Test-Path $tar) {
        docker run --rm --name "aihub-mig-load-$($pair[1])" -v "$($pair[1]):/data" -v "${pack}:/dump" --entrypoint sh `
            $pair[2] -c "cd /dump && tar xf $($pair[0]) -C /data"
        if ($LASTEXITCODE -ne 0) { Die "卷恢复失败: $($pair[1])" }
    } elseif ($Force) {
        Write-Warning "缺 $($pair[0])（-Force 跳过）— 新机器上该卷为空"
    } else {
        Die "迁移包缺 $($pair[0])（数据卷导出缺失）— 用 -Force 确认跳过，否则向导出方核对"
    }
}

# ---- 启动（各栈在各自目录，避免路径错位） ----
if ($Rehearsal) {
    Step "-Rehearsal: 生成 override（剥离 container_name、重映射端口）"
    $wecomOverride = Join-Path $ProjectDir "rehearsal-override.yml"
    @"
services:
  wecom-mcp:
    container_name: !override ""
    ports: !override ["127.0.0.1:18331:8331"]
"@ | Set-Content $wecomOverride -Encoding UTF8
    $aihubOverride = Join-Path $aihubDir "rehearsal-override.yml"
    @"
services:
  app:
    container_name: !override ""
    ports: !override ["127.0.0.1:18001:8001"]
  postgres:
    container_name: !override ""
    ports: !override ["15482:5432"]
  redis:
    container_name: !override ""
    ports: !override ["16382:6379"]
  minio:
    container_name: !override ""
    ports: !override ["19082:9000", "19083:9001"]
  bge-m3:
    container_name: !override ""
    ports: !override ["18082:80"]
  bge-reranker:
    container_name: !override ""
    ports: !override ["18083:80"]
"@ | Set-Content $aihubOverride -Encoding UTF8
}

Step "启动 aihub 全栈"
Push-Location $aihubDir
try {
    if ($Rehearsal) { docker compose -f docker/docker-compose.yml -f rehearsal-override.yml -p aihub-rehearsal up -d }
    else { docker compose -f docker/docker-compose.yml up -d }
    if ($LASTEXITCODE -ne 0) { Die "aihub compose up 失败" }
} finally { Pop-Location }

Step "启动本项目"
Push-Location $ProjectDir
try {
    if ($Rehearsal) { docker compose -f docker-compose.yml -f rehearsal-override.yml -p wecom-rehearsal up -d }
    else { docker compose up -d }
    if ($LASTEXITCODE -ne 0) { Die "本项目 compose up 失败" }
} finally { Pop-Location }

# ---- 验证 ----
Step "health 探活（等待最多 120 秒）"
$wecomPort = if ($Rehearsal) { 18331 } else { 8331 }
$ok = $false
foreach ($i in 1..24) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$wecomPort/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
}
if (-not $ok) { Die "本项目 /health 未就绪 — 查 docker compose logs" }
Write-Host "  ✓ 本项目 health 200"

Step "容器内 RAG 连通性检查（KTD8，docker exec 容器视角）"
$ragPort = if ($Rehearsal) { 18001 } else { 8001 }
$execArgs = if ($Rehearsal) { @("-p", "wecom-rehearsal") } else { @() }
try {
    Push-Location $ProjectDir
    docker compose @execArgs exec -T wecom-mcp python -c "import os,urllib.request; url=os.environ['RAG_API_URL'].rsplit('/api/',1)[0]+'/api/health'; r=urllib.request.urlopen(url, timeout=10); print('rag-health', r.status); assert r.status==200"
    Pop-Location
    if ($LASTEXITCODE -ne 0) { throw "容器内检查返回 $LASTEXITCODE" }
    Write-Host "  ✓ 容器内 RAG 连通"
} catch {
    try { Pop-Location } catch {}
    Die "容器内 RAG 连通检查失败: $($_.Exception.Message) — 检查 aihub 容器日志与 KTD8 转换"
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "恢复完成。剩余人工验证单（MIGRATION.md）:"
Write-Host "  [ ] 拉取消息: POST http://127.0.0.1:$wecomPort/api/pull"
Write-Host "  [ ] 群问答:   群内 @智能小助手 测试（企微渠道）"
Write-Host "  [ ] 仪表盘:   http://127.0.0.1:$wecomPort/"
Write-Host "  [ ] wxbridge: 按部署文档启动（若已配置）"
if ($Rehearsal) { Write-Host "  [ ] 演练完成: docker compose -p wecom-rehearsal down && docker compose -p aihub-rehearsal -f aihub/docker/docker-compose.yml down 并清理卷" }
Write-Host "  [ ] 退役清机: 按 MIGRATION.md 退役章节执行（勿跳过）"
Write-Host "========================================" -ForegroundColor Green
