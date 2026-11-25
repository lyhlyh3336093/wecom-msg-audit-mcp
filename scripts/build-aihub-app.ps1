# aihub 应用镜像构建（黑窗实时进度）
# 用法: Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',<本文件路径>
Set-Location 'D:\WorkSpace\aihub-knowledge\docker'
Write-Host '=== aihub-knowledge 应用镜像构建 ===' -ForegroundColor Cyan
Write-Host "开始时间: $(Get-Date -Format 'HH:mm:ss')"

docker compose build app --progress=plain

if ($LASTEXITCODE -eq 0) {
    Write-Host '=== 构建成功 ===' -ForegroundColor Green
    docker images aihub-knowledge-app --format '镜像: {{.Repository}}:{{.Tag}}  大小: {{.Size}}'
    Write-Host '验证: docker compose -f docker-compose.yml up -d app  然后 curl http://127.0.0.1:8001/api/health'
} else {
    Write-Host "=== 构建失败（exit $LASTEXITCODE，错误见上方）===" -ForegroundColor Red
}
Write-Host "结束时间: $(Get-Date -Format 'HH:mm:ss')  此窗口保留，按 Enter 关闭"
Read-Host
