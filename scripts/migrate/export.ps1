# 迁移导出脚本（U2）— 在当前机器执行，产出加密迁移包
# 用法:  powershell -ExecutionPolicy Bypass -File scripts\migrate\export.ps1 [-OutputDir <目录>] [-SkipStopContainers]
# 产物:  <OutputDir>\migration_pack.7z（AES-256 加密归档）+ manifest.json + 解压密码提示
#
# 步骤（对应计划 U2 Approach）:
#   ⓪ 停止写入方（本项目 compose 全部 + aihub 的 app 容器）；pg/minio/bge 保持运行，
#      pg_dump 走运行中库的一致性快照，MinIO 导出时单独短暂停止
#   ① docker save 全部相关镜像
#   ② docker exec aihub-knowledge-pg pg_dump（运行中容器内执行，版本天然匹配）
#   ③ 打包 data/（7z）与 aihub 数据卷（MinIO 停容器后 tar；BGE 模型卷只读直接 tar）
#   ④ 拷贝 .env（双方）、sdk/、secrets/、compose 文件（aihub 改名落盘避免覆盖）
#   ⑤ 生成 manifest.json（清单 + SHA256 校验和）
#   ⑥ 压缩为 AES-256 加密归档（随机强密码；无 7z 直接中止，不落明文）
#
# 安全: 迁移包含全部凭据与已解密聊天记录 — 解压密码必须经独立渠道传递，绝不与归档同介质存放。
# 任何步骤失败都会在 finally 中重启容器后再退出。

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

$sevenZip = Get-Command 7z -ErrorAction SilentlyContinue
if (-not $sevenZip) {
    Write-Host "[export] 未安装 7z — 迁移包含全部凭据与解密聊天记录，拒绝生成明文产物。" -ForegroundColor Red
    Write-Host "[export] 安装: winget install 7zip.7zip  然后重跑本脚本。" -ForegroundColor Red
    exit 1
}

$images = @("wecom-msg-audit-mcp:latest", "aihub-knowledge-app:latest",
            "docker.m.daocloud.io/pgvector/pgvector:pg17", "redis:7-alpine",
            "docker.m.daocloud.io/minio/minio:latest",
            "ghcr.io/huggingface/text-embeddings-inference:cpu-1.6")
$aihubStopped = $false
$stopStamp = (Get-Date).ToString("o")
$restartStamp = ""

try {
    # ---- ⓪ 停止写入方 ----
    if (-not $SkipStopContainers) {
        Step "⓪ 停止写入方容器（本项目全部 + aihub app；pg/minio 保持运行）"
        docker compose stop 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "本项目 compose stop 返回 $LASTEXITCODE（可能本就未运行，继续）" }
        if (Test-Path $AihubComposePath) {
            docker compose -f $AihubComposePath stop app 2>&1 | Out-Null
            $script:aihubStopped = ($LASTEXITCODE -eq 0)
            if (-not $aihubStopped) { Write-Warning "aihub app stop 返回 $LASTEXITCODE（可能本就未运行）" }
        }
    } else {
        Write-Warning "-SkipStopContainers: 容器持续写入，快照可能撕裂（仅限演练）"
    }

    # ---- ① docker save 镜像 ----
    Step "① 导出 Docker 镜像"
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
        throw "镜像缺失"
    }
    foreach ($img in $images) {
        $tar = Join-Path $work ("img_" + ($img -replace '[/:]', '_') + ".tar")
        Step "  save $img"
        docker save -o $tar $img
        if ($LASTEXITCODE -ne 0) { throw "docker save 失败: $img" }
    }

    # ---- ② 容器内 pg_dump（pg 容器保持运行，一致性快照） ----
    Step "② 导出知识库 Postgres（docker exec 运行中容器）"
    docker exec aihub-knowledge-pg pg_dump -U aihub -d aihub_knowledge -F c -f /tmp/aihub_knowledge.dump
    if ($LASTEXITCODE -ne 0) { throw "pg_dump 失败（容器 aihub-knowledge-pg 未运行或凭据不符）" }
    docker cp aihub-knowledge-pg:/tmp/aihub_knowledge.dump (Join-Path $work "aihub_knowledge.dump")
    if ($LASTEXITCODE -ne 0) { throw "pg_dump 产物拷出失败" }

    # ---- ③ data/ 与 aihub 数据卷 ----
    Step "③ 打包 data/（7z）与 aihub 数据卷"
    if (Test-Path (Join-Path $RepoRoot "data")) {
        & 7z a -t7z -mx=3 (Join-Path $work "wecom_data.7z") (Join-Path $RepoRoot "data\*") | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "data/ 打包失败" }
    } else {
        Write-Warning "data/ 不存在，跳过（首次部署前迁移？）"
    }

    Step "  导出 MinIO 卷（短暂停止 minio 容器保证一致性）"
    docker stop aihub-knowledge-minio | Out-Null
    docker volume inspect aihub-knowledge_minio_data *> $null
    if ($LASTEXITCODE -ne 0) { throw "数据卷 aihub-knowledge_minio_data 不存在 — docker volume ls 核对卷名" }
    docker run --rm --name aihub-mig-minio -v aihub-knowledge_minio_data:/data -v "${work}:/dump" --entrypoint sh `
        docker.m.daocloud.io/minio/minio:latest -c "cd /data && tar cf /dump/minio_data.tar ."
    if ($LASTEXITCODE -ne 0) { throw "MinIO 卷导出失败" }
    docker start aihub-knowledge-minio | Out-Null

    foreach ($pair in @(@("aihub-knowledge_bge_m3_data", "bge_m3_data.tar"),
                        @("aihub-knowledge_bge_reranker_data", "bge_reranker_data.tar"))) {
        Step "  导出 BGE 模型卷 $($pair[0])（只读 tar）"
        docker volume inspect $pair[0] *> $null
        if ($LASTEXITCODE -ne 0) { throw "数据卷 $($pair[0]) 不存在 — docker volume ls 核对卷名" }
        docker run --rm --name "aihub-mig-$($pair[1])" -v "$($pair[0]):/data" -v "${work}:/dump" --entrypoint sh `
            docker.m.daocloud.io/pgvector/pgvector:pg17 -c "cd /data && tar cf /dump/$($pair[1]) ."
        if ($LASTEXITCODE -ne 0) { throw "卷导出失败: $($pair[0])" }
    }

    # ---- ④ 配置与密钥 ----
    Step "④ 拷贝 .env / sdk / secrets / compose 文件"
    Copy-Item (Join-Path $RepoRoot ".env") (Join-Path $work "wecom.env") -Force
    if (-not (Test-Path $AihubEnvPath)) { throw "aihub .env 不存在: $AihubEnvPath" }
    Copy-Item $AihubEnvPath (Join-Path $work "aihub.env") -Force
    Copy-Item (Join-Path $RepoRoot "docker-compose.yml") $work -Force
    Copy-Item $AihubComposePath (Join-Path $work "aihub-docker-compose.yml") -Force
    foreach ($dir in @("sdk", "secrets")) {
        if (Test-Path (Join-Path $RepoRoot $dir)) {
            & 7z a -t7z -mx=3 (Join-Path $work "$dir.7z") (Join-Path $RepoRoot "$dir\*") | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "$dir/ 打包失败" }
        } else {
            Write-Warning "$dir/ 不存在，跳过"
        }
    }
    if (Test-Path "D:\WorkSpace\aihub-knowledge\docker\Dockerfile") {
        New-Item -ItemType Directory -Path (Join-Path $work "aihub-docker") | Out-Null
        Copy-Item D:\WorkSpace\aihub-knowledge\docker\Dockerfile (Join-Path $work "aihub-docker\Dockerfile") -Force
        if (Test-Path "D:\WorkSpace\aihub-knowledge\.dockerignore") {
            Copy-Item D:\WorkSpace\aihub-knowledge\.dockerignore (Join-Path $work "aihub-docker\.dockerignore") -Force
        }
    }
}
finally {
    # ---- 重启写入方（无论成败都执行） ----
    if (-not $SkipStopContainers) {
        Step "重启写入方容器"
        docker compose start 2>&1 | Out-Null
        if ($script:aihubStopped -and (Test-Path $AihubComposePath)) {
            docker compose -f $AihubComposePath start app 2>&1 | Out-Null
        }
        $restartStamp = (Get-Date).ToString("o")
    }
}

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
    restarted_at = $restartStamp
    images = $images
    files = @($entries)
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $work "manifest.json") -Encoding UTF8

# ---- ⑥ 加密归档（随机强密码） ----
Step "⑥ 压缩加密归档（随机密码）"
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 24
$rng.GetBytes($bytes)
$password = [Convert]::ToBase64String($bytes).TrimEnd('=')
& 7z a -t7z -mx=5 -mhe=on "-p$password" (Join-Path $OutputDir "migration_pack.7z") "$work\*" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "[export] 7z 打包失败" -ForegroundColor Red; exit 1 }
Remove-Item $work -Recurse -Force

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "迁移包已生成: $OutputDir\migration_pack.7z（内容已加密，含文件名加密）"
Write-Host "解压密码: $password  ← 立即复制到密码管理器，不要存在本目录！" -ForegroundColor Yellow
Write-Host "manifest.json（明文清单，仅文件名与校验和，无敏感值）"
Write-Host "警告: 迁移包含全部凭据与已解密聊天记录，遗失即重大泄露" -ForegroundColor Red
Write-Host "========================================" -ForegroundColor Green
