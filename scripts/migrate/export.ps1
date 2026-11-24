# 迁移导出脚本（U2）— 在当前机器执行，产出加密迁移包
# 用法:  powershell -ExecutionPolicy Bypass -File scripts\migrate\export.ps1 [-OutputDir <目录>] [-SkipStopContainers]
# 产物:  <OutputDir>\migration_pack.7z（AES-256 加密归档）+ manifest.json + 解压密码提示
#
# 步骤（对应计划 U2 Approach）:
#   ⓪ 停止写入源（本项目 + aihub 两个 compose 项目），防 WAL/JSONL 撕裂快照
#   ① docker save 全部相关镜像
#   ② 容器内 pg_dump 知识库（版本与容器内 Postgres 匹配）
#   ③ 打包 data/ 与 aihub 数据卷（MinIO 桶 + BGE 模型卷）
#   ④ 拷贝 .env（双方）、sdk/、secrets/、compose 文件
#   ⑤ 生成 manifest.json（清单 + SHA256 校验和）
#   ⑥ 压缩为 AES-256 加密归档（7z；无 7z 时降级 zip 并提示）
#
# 安全: 迁移包含全部凭据与已解密聊天记录 — 解压密码必须经独立渠道传递，绝不与归档同介质存放。

param(
    [string]$OutputDir = ".\migration_pack",
    [switch]$SkipStopContainers,
    [string]$AihubComposePath = "D:\WorkSpace\aihub-knowledge\docker\docker-compose.yml",
    [string]$AihubEnvPath = "D:\WorkSpace\aihub-knowledge\.env"
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "[export] $msg" -ForegroundColor Cyan }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot

if (Test-Path $OutputDir) {
    Write-Host "[export] 输出目录已存在: $OutputDir — 删除重建" -ForegroundColor Yellow
    Remove-Item $OutputDir -Recurse -Force
}
New-Item -ItemType Directory -Path $OutputDir | Out-Null
$work = Join-Path $OutputDir "work"
New-Item -ItemType Directory -Path $work | Out-Null

# ---- ⓪ 停止写入源 ----
if (-not $SkipStopContainers) {
    Step "⓪ 停止写入源容器（防撕裂快照）"
    docker compose stop 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "本项目 compose stop 返回 $LASTEXITCODE（可能本就未运行，继续）" }
    if (Test-Path $AihubComposePath) {
        docker compose -f $AihubComposePath stop 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "aihub compose stop 返回 $LASTEXITCODE（可能本就未运行，继续）" }
    }
} else {
    Write-Warning "-SkipStopContainers: 容器持续写入，快照可能撕裂（仅限演练）"
}
$stopStamp = (Get-Date).ToString("o")

# ---- ① docker save 镜像 ----
Step "① 导出 Docker 镜像"
$images = @("wecom-msg-audit-mcp:latest", "aihub-knowledge-app:latest",
            "docker.m.daocloud.io/pgvector/pgvector:pg17", "redis:7-alpine",
            "docker.m.daocloud.io/minio/minio:latest",
            "ghcr.io/huggingface/text-embeddings-inference:cpu-1.6")
$missing = @()
foreach ($img in $images) {
    docker image inspect $img *> $null
    if ($LASTEXITCODE -ne 0) { $missing += $img }
}
if ($missing.Count -gt 0) {
    Write-Host "[export] 以下镜像不存在，中止:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  - $_" }
    Write-Host "[export] 本机现有镜像:" -ForegroundColor Yellow
    docker images --format "{{.Repository}}:{{.Tag}}"
    exit 1
}
foreach ($img in $images) {
    $tar = Join-Path $work ("img_" + ($img -replace '[/:]', '_') + ".tar")
    Step "  save $img"
    docker save -o $tar $img
    if ($LASTEXITCODE -ne 0) { Write-Host "[export] docker save 失败: $img" -ForegroundColor Red; exit 1 }
}

# ---- ② 容器内 pg_dump ----
Step "② 导出知识库 Postgres（容器内 pg_dump，保证版本匹配）"
# 从 aihub compose 恢复临时 pg 容器执行 dump（服务此刻已停止，用独立一次性容器挂同一数据卷）
$pgTag = "docker.m.daocloud.io/pgvector/pgvector:pg17"
docker run --rm --name aihub-mig-dump -v aihub-knowledge_pg_data:/var/lib/postgresql/data `
    -v "${work}:/dump" $pgTag bash -c "pg_dump -U aihub -d aihub_knowledge -F c -f /dump/aihub_knowledge.dump"
if ($LASTEXITCODE -ne 0) {
    # 数据卷名可能带项目前缀，回退列出卷供核对
    Write-Host "[export] pg_dump 失败（卷名可能与默认不同）。本机卷列表:" -ForegroundColor Red
    docker volume ls --format "{{.Name}}" | Select-String "pg"
    exit 1
}

# ---- ③ data/ 与 aihub 数据卷 ----
Step "③ 打包本项目 data/ 与 aihub MinIO/BGE 卷"
if (Test-Path (Join-Path $RepoRoot "data")) {
    Compress-Archive -Path (Join-Path $RepoRoot "data\*") -DestinationPath (Join-Path $work "wecom_data.zip") -Force
} else {
    Write-Warning "data/ 不存在，跳过（首次部署前迁移？）"
}
docker run --rm --name aihub-mig-minio -v aihub-knowledge_minio_data:/data -v "${work}:/dump" --entrypoint sh `
    docker.m.daocloud.io/minio/minio:latest -c "cd /data && tar cf /dump/minio_data.tar ."
docker run --rm --name aihub-mig-bge1 -v aihub-knowledge_bge_m3_data:/data -v "${work}:/dump" --entrypoint sh `
    $pgTag -c "cd /data && tar cf /dump/bge_m3_data.tar ."
docker run --rm --name aihub-mig-bge2 -v aihub-knowledge_bge_reranker_data:/data -v "${work}:/dump" --entrypoint sh `
    $pgTag -c "cd /data && tar cf /dump/bge_reranker_data.tar ."

# ---- ④ 配置与密钥 ----
Step "④ 拷贝 .env / sdk / secrets / compose 文件"
Copy-Item (Join-Path $RepoRoot ".env") (Join-Path $work "wecom.env") -Force
Copy-Item $AihubEnvPath (Join-Path $work "aihub.env") -Force
Copy-Item (Join-Path $RepoRoot "docker-compose.yml") $work -Force
Copy-Item $AihubComposePath $work -Force
foreach ($dir in @("sdk", "secrets")) {
    if (Test-Path (Join-Path $RepoRoot $dir)) {
        Compress-Archive -Path (Join-Path $RepoRoot "$dir\*") -DestinationPath (Join-Path $work "$dir.zip") -Force
    } else {
        Write-Warning "$dir/ 不存在，跳过"
    }
}
if (Test-Path "D:\WorkSpace\aihub-knowledge\docker\Dockerfile") {
    New-Item -ItemType Directory -Path (Join-Path $work "aihub-docker") | Out-Null
    Copy-Item D:\WorkSpace\aihub-knowledge\docker\Dockerfile (Join-Path $work "aihub-docker\Dockerfile") -Force
    Copy-Item D:\WorkSpace\aihub-knowledge\.dockerignore (Join-Path $work "aihub-docker\.dockerignore") -Force
}

# ---- 重启写入源 ----
if (-not $SkipStopContainers) {
    Step "重启写入源容器"
    docker compose start 2>&1 | Out-Null
    if (Test-Path $AihubComposePath) { docker compose -f $AihubComposePath start 2>&1 | Out-Null }
}
$startStamp = (Get-Date).ToString("o")

# ---- ⑤ manifest.json ----
Step "⑤ 生成 manifest.json（SHA256 校验和）"
$files = Get-ChildItem $work -File
$entries = foreach ($f in $files) {
    $hash = (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()
    [ordered]@{ file = $f.Name; bytes = $f.Length; sha256 = $hash }
}
$manifest = [ordered]@{
    exported_at = (Get-Date).ToString("o")
    stopped_at = $stopStamp
    restarted_at = $startStamp
    images = $images
    files = @($entries)
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $work "manifest.json") -Encoding UTF8

# ---- ⑥ 加密归档 ----
Step "⑥ 压缩加密归档"
$sevenZip = Get-Command 7z -ErrorAction SilentlyContinue
if ($sevenZip) {
    $plain = Join-Path $OutputDir "work"
    $stamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
    & 7z a -t7z -mx=5 "-p$stamp" (Join-Path $OutputDir "migration_pack.7z") "$plain\*" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "[export] 7z 打包失败" -ForegroundColor Red; exit 1 }
    Remove-Item $plain -Recurse -Force
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "迁移包已生成: $OutputDir\migration_pack.7z"
    Write-Host "解压密码: $stamp  ← 立即复制到密码管理器，不要存在本目录！" -ForegroundColor Yellow
    Write-Host "本目录含 manifest.json（明文清单，无敏感值）"
    Write-Host "警告: 迁移包含全部凭据与已解密聊天记录，遗失即重大泄露" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Green
} else {
    # 无 7z：保留明文 work 目录并强提示
    Move-Item $work (Join-Path $OutputDir "work_UNENCRYPTED")
    Write-Host ""
    Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
    Write-Host "未安装 7z，产物为明文目录: $OutputDir\work_UNENCRYPTED"
    Write-Host "该目录含全部凭据与解密聊天记录 — 请立即安装 7-Zip 后重跑，或人工加密后转移"
    Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
}
