#Requires -Version 5.1
<#
.SYNOPSIS
    企业微信会话存档：RSA 密钥生成、SDK 安装校验、凭据写入、联通性自检。

.DESCRIPTION
    必须由人登录企业微信管理后台完成、本脚本无法代做的部分：
      1. 开通「会话内容存档」并设置存档范围（成员/部门）
      2. 上传本脚本生成的 public_key.pem
      3. 抄录 CORP_ID 与「会话内容存档」Secret
      4. 把部署机的出口公网 IP 加入企业可信 IP
    本脚本负责其余全部可自动化环节，并在 -Verify 时逐项检查结果。

.EXAMPLE
    .\setup-wecom.ps1 -GenKeys
    生成 secrets/private_key.pem 与 public_key.pem，并把公钥打印出来供上传。

.EXAMPLE
    .\setup-wecom.ps1 -InstallSdk .\WeWorkFinanceSdk_C_Linux.tar.gz
    校验 ELF 架构后安装到 sdk/libWeWorkFinanceSdk_C.so，并试加载。

.EXAMPLE
    .\setup-wecom.ps1 -SetCredential
    掩码输入并写入 .env。

.EXAMPLE
    .\setup-wecom.ps1 -Verify -ForceRecreate -SmokePull
    全链路自检：文件 → gettoken → 重建容器 → 真实拉取 1 条。
#>
[CmdletBinding()]
param(
    [switch]$GenKeys,
    [switch]$InstallSdk,
    [string]$SdkPath,
    [switch]$SetCredential,
    [switch]$Console,
    [switch]$NoBrowser,
    [switch]$Verify,
    [switch]$ForceRecreate,
    [switch]$SmokePull,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$EnvFile = Join-Path $Root '.env'
$SdkDir = Join-Path $Root 'sdk'
$SecretsDir = Join-Path $Root 'secrets'
$SoName = 'libWeWorkFinanceSdk_C.so'
$SoPath = Join-Path $SdkDir $SoName
$PrivPath = Join-Path $SecretsDir 'private_key.pem'
$PubPath = Join-Path $SecretsDir 'public_key.pem'
$RequiredKeys = @('CORP_ID', 'MSGAUDIT_SECRET')

function Write-Step([string]$Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "  [WARN] $Text" -ForegroundColor Yellow }
function Write-Err([string]$Text) { Write-Host "  [FAIL] $Text" -ForegroundColor Red }

function Get-EnvValue {
    param([string]$Key, [string]$Path = $EnvFile)
    if (-not (Test-Path $Path)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match "^\s*$([regex]::Escape($Key))=(.*)$") {
            return $matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

function Set-EnvValue {
    param([string]$Key, [string]$Value, [string]$Path = $EnvFile)
    if (-not (Test-Path $Path)) { throw "找不到 .env：$Path（先执行 Copy-Item .env.example .env）" }
    $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8)
    $found = $false
    $lines = $lines | ForEach-Object {
        if ($_ -match "^\s*$([regex]::Escape($Key))=") { $found = $true; "$Key=$Value" } else { $_ }
    }
    if (-not $found) { $lines = $lines + "$Key=$Value" }
    # PowerShell 5 的 Out-File -Encoding UTF8 会写 BOM，破坏首行 key，必须用无 BOM 写入
    [System.IO.File]::WriteAllLines((Resolve-Path $Path).Path, $lines, (New-Object System.Text.UTF8Encoding $false))
}

function Read-Masked([string]$Prompt) {
    $sec = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function ConvertTo-WslPath([string]$WinPath) {
    return '/mnt/' + $WinPath.Substring(0, 1).ToLower() + $WinPath.Substring(2).Replace('\', '/')
}

function Test-ElfX64([string]$Path) {
    $b = [System.IO.File]::ReadAllBytes($Path)
    if ($b.Length -lt 20) { return @{ Ok = $false; Reason = '文件过小，不是有效的 .so' } }
    if (-not ($b[0] -eq 0x7F -and $b[1] -eq 0x45 -and $b[2] -eq 0x4C -and $b[3] -eq 0x46)) {
        return @{ Ok = $false; Reason = '不是 ELF 文件（可能是 Windows DLL 或 macOS dylib）' }
    }
    if ($b[4] -ne 2) { return @{ Ok = $false; Reason = '不是 64 位 ELF' } }
    if (-not ($b[18] -eq 0x3E -and $b[19] -eq 0x00)) {
        return @{ Ok = $false; Reason = '架构不是 x86_64（当前镜像为 linux/amd64）' }
    }
    return @{ Ok = $true; Reason = 'ELF 64-bit x86_64' }
}

function Invoke-InContainer([string]$PythonCode) {
    # PowerShell 5 下内嵌双引号会被吞掉，且 2>&1 会把 stderr 变成 NativeCommandError
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & docker compose exec -T wecom-mcp python -c $PythonCode 2>&1
        if ($LASTEXITCODE -ne 0) {
            $out = & docker compose run --rm --no-deps -T wecom-mcp python -c $PythonCode 2>&1
        }
        return ($out | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $prev
    }
}

# ----------------------------------------------------------------------------
# 1. RSA 密钥对
# ----------------------------------------------------------------------------
function New-RsaKeyPair {
    Write-Step '生成 RSA 2048 密钥对'
    New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null

    if ((Test-Path $PrivPath) -and -not $Force) {
        throw "已存在 $PrivPath。重新生成会使已上传公钥失效，确认请加 -Force"
    }

    $done = $false
    $wslPriv = ConvertTo-WslPath $PrivPath
    $wslPub = ConvertTo-WslPath $PubPath

    # WSL 的 bash -l 会向 stderr 输出 mesg 警告，Strict 模式会当成异常，这里临时放宽
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $ossl = (& wsl -- bash -c 'command -v openssl' 2>$null | Out-String).Trim()
        if ($ossl) {
            Write-Host '  使用 WSL openssl 生成（与官方文档命令一致）'
            & wsl -- bash -c "openssl genrsa -out '$wslPriv' 2048 && openssl rsa -in '$wslPriv' -pubout -out '$wslPub'" 2>&1 | Out-Null
            $done = (Test-Path $PrivPath) -and (Test-Path $PubPath)
        }

        if (-not $done) {
            Write-Host '  回退到容器内 pycryptodome 生成'
            $code = 'from Crypto.PublicKey import RSA; k=RSA.generate(2048); open(''/app/secrets/private_key.pem'',''wb'').write(k.export_key(pkcs=1)); open(''/app/secrets/public_key.pem'',''wb'').write(k.publickey().export_key())'
            Invoke-InContainer $code | Out-Null
            $done = (Test-Path $PrivPath) -and (Test-Path $PubPath)
        }
    } finally {
        $ErrorActionPreference = $prev
    }

    if (-not $done) { throw '密钥生成失败：WSL 与容器两种方式均不可用' }

    Write-Ok "私钥: $PrivPath"
    Write-Ok "公钥: $PubPath"
    Write-Host ''
    Write-Host '  下一步：把下面这段完整公钥粘贴到 管理后台 -> 会话内容存档 -> 公钥配置' -ForegroundColor Yellow
    Write-Host '  （上传后后台的 publickey_ver 会递增，旧私钥即刻失效）' -ForegroundColor Yellow
    Write-Host ''
    Get-Content -LiteralPath $PubPath | ForEach-Object { Write-Host "    $_" }
}

# ----------------------------------------------------------------------------
# 2. SDK 安装
# ----------------------------------------------------------------------------
function Install-Sdk {
    Write-Step '安装官方 SDK 动态库'
    if (-not $SdkPath) { throw '缺少 -SdkPath 参数，指向下载得到的 .so 或 .tar.gz' }
    if (-not (Test-Path $SdkPath)) { throw "文件不存在: $SdkPath" }

    New-Item -ItemType Directory -Force -Path $SdkDir | Out-Null
    $item = Get-Item -LiteralPath $SdkPath
    $tmpDir = $null

    try {
        if ($item.Name -match '\.(tar\.gz|tgz)$') {
            $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ('wecom_sdk_' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $tmpDir | Out-Null
            tar -xzf $item.FullName -C $tmpDir
            $so = Get-ChildItem -Path $tmpDir -Recurse -Filter '*.so' | Select-Object -First 1
            if (-not $so) { throw '压缩包内未找到 .so 文件' }
            Copy-Item -LiteralPath $so.FullName -Destination $SoPath -Force
        } elseif ($item.Extension -eq '.zip') {
            $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ('wecom_sdk_' + [guid]::NewGuid().ToString('N'))
            Expand-Archive -LiteralPath $item.FullName -DestinationPath $tmpDir -Force
            $so = Get-ChildItem -Path $tmpDir -Recurse -Filter '*.so' | Select-Object -First 1
            if (-not $so) { throw '压缩包内未找到 .so 文件' }
            Copy-Item -LiteralPath $so.FullName -Destination $SoPath -Force
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $SoPath -Force
        }
    } finally {
        if ($tmpDir -and (Test-Path $tmpDir)) { Remove-Item $tmpDir -Recurse -Force }
    }

    $check = Test-ElfX64 $SoPath
    if (-not $check.Ok) { throw "SDK 校验失败：$($check.Reason)" }
    Write-Ok "已安装: $SoPath ($($check.Reason))"

    Write-Host '  在容器内试加载 ...'
    $loadCode = 'import ctypes; ctypes.CDLL(''/app/sdk/libWeWorkFinanceSdk_C.so''); print(''LOAD_OK'')'
    $out = Invoke-InContainer $loadCode
    if ($out -match 'LOAD_OK') {
        Write-Ok '容器内加载成功（glibc 兼容）'
    } else {
        Write-Err "容器内加载失败：$out"
        Write-Warn2 '常见原因：glibc 版本过低/过高、缺少依赖库，可用 ldd 进一步排查'
    }
}

# ----------------------------------------------------------------------------
# 3. 凭据写入
# ----------------------------------------------------------------------------
function Set-Credential {
    Write-Step '写入企业微信凭据'
    if (-not (Test-Path $EnvFile)) {
        Copy-Item (Join-Path $Root '.env.example') $EnvFile
        Write-Ok '已从 .env.example 创建 .env'
    }

    $corpId = Read-Host -Prompt '企业 ID (CORP_ID，管理后台 -> 我的企业)'
    if ($corpId) { Set-EnvValue 'CORP_ID' $corpId; Write-Ok 'CORP_ID 已写入' }

    $auditSecret = Read-Masked '会话内容存档 Secret (输入不回显)'
    if ($auditSecret) { Set-EnvValue 'MSGAUDIT_SECRET' $auditSecret; Write-Ok 'MSGAUDIT_SECRET 已写入' }

    $extSecret = Read-Masked '客户联系 Secret (可选，外部客户群需要；直接回车跳过)'
    if ($extSecret) { Set-EnvValue 'EXTERNAL_CONTACT_SECRET' $extSecret; Write-Ok 'EXTERNAL_CONTACT_SECRET 已写入' }

    Write-Warn2 '.env 已被 .gitignore 忽略；凭据变更需重建容器才会生效'
}

# ----------------------------------------------------------------------------
# 3.5 管理后台人工步骤辅助（公钥上传 / 可信 IP 均无开放 API）
# ----------------------------------------------------------------------------
function Get-PublicIp {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $hostIp = $null
        $ctIp = $null
        try {
            $raw = (Invoke-WebRequest -Uri 'https://myip.ipip.net' -UseBasicParsing -TimeoutSec 10).Content
            if ($raw -match '(\d{1,3}(?:\.\d{1,3}){3})') { $hostIp = $matches[1] }
        } catch { }
        $code = 'import urllib.request; print(urllib.request.urlopen(''https://myip.ipip.net'', timeout=10).read().decode(''utf-8'').strip())'
        $out = Invoke-InContainer $code
        if ($out -match '(\d{1,3}(?:\.\d{1,3}){3})') { $ctIp = $matches[1] }
        return @{ Host = $hostIp; Container = $ctIp }
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Show-ConsoleAssist {
    Write-Step '公钥上传与可信 IP：后台人工操作辅助'
    Write-Host '  企业微信未开放这两项的 API，只能登录管理后台配置，脚本负责把准备工作做齐。' -ForegroundColor Yellow

    if (-not (Test-Path $PubPath)) { throw "公钥不存在：$PubPath（先跑 -GenKeys）" }
    $pub = (Get-Content -LiteralPath $PubPath -Raw).Trim()
    $copied = $false
    try { Set-Clipboard -Value $pub; $copied = $true } catch { }
    if ($copied) { Write-Ok '公钥全文已复制到剪贴板，可整段粘贴' }
    else { Write-Warn2 '剪贴板不可用（非交互会话），请手动复制下方内容' }
    Write-Host ''
    Write-Host $pub -ForegroundColor DarkGray
    Write-Host ''

    Write-Step '出口公网 IP 探测'
    $ips = Get-PublicIp
    if ($ips.Host) { Write-Ok "宿主出口 IP: $($ips.Host)" } else { Write-Warn2 '宿主出口 IP 探测失败（myip.ipip.net 不可达）' }
    if ($ips.Container) {
        if ($ips.Container -eq $ips.Host) { Write-Ok "容器出口 IP: $($ips.Container)（与宿主一致，只填一个即可）" }
        else { Write-Warn2 "容器出口 IP: $($ips.Container)（与宿主不同，两者都要加入白名单）" }
    } else {
        Write-Warn2 '容器出口 IP 探测失败'
    }

    $ipList = ''
    if ($ips.Host) { $ipList = $ips.Host }
    if ($ips.Container -and $ips.Container -ne $ips.Host) {
        if ($ipList) { $ipList = "$ipList 与 $($ips.Container)" } else { $ipList = $ips.Container }
    }

    Write-Step '后台操作路径'
    Write-Host '  1) 上传公钥'
    Write-Host '     安全与管理 -> 管理工具 -> 会话内容存档 -> 消息加密公钥'
    Write-Host '     粘贴剪贴板内容并保存；保存后 publickey_ver 递增，旧私钥立即失效'
    Write-Host ''
    Write-Host '  2) 配置企业可信 IP'
    if ($ipList) { Write-Host "     在对应应用的「企业可信IP」中填入: $ipList" }
    else { Write-Host '     在对应应用的「企业可信IP」中填入上方探测到的公网 IP' }
    Write-Host '     若找不到入口，在管理后台顶部搜索「可信IP」'

    if ($NoBrowser) {
        Write-Warn2 '已跳过打开管理后台（-NoBrowser）'
    } else {
        Start-Process 'https://work.weixin.qq.com/wework_admin/frame#/manageTools'
        Write-Ok '已打开管理后台「管理工具」页'
    }

    Write-Step '完成后回读校验'
    Write-Host '  .\setup-wecom.ps1 -Verify -ForceRecreate -SmokePull'
    Write-Host '  gettoken 不再返回 60020 即说明可信 IP 已生效'
}

# ----------------------------------------------------------------------------
# 4. 联通性自检
# ----------------------------------------------------------------------------
function Test-GetToken([string]$CorpId, [string]$Secret, [string]$Label) {
    $uri = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={0}&corpsecret={1}' -f $CorpId, $Secret
    try {
        $resp = Invoke-RestMethod -Uri $uri -Method Get -TimeoutSec 15
    } catch {
        Write-Err "$Label gettoken 网络失败：$($_.Exception.Message)"
        return $false
    }

    switch ($resp.errcode) {
        0 {
            Write-Ok "$Label gettoken 成功（Secret 有效、出口 IP 已在可信列表）"
            return $true
        }
        40001 {
            Write-Err "$Label Secret 无效（不匹配该 corpid，或用了普通应用 Secret）"
            return $false
        }
        60020 {
            Write-Err "$Label 出口 IP 不在企业可信 IP 白名单（errcode=60020）"
            Write-Warn2 'Docker 容器经宿主 NAT 出网，需把宿主公网出口 IP 加入后台白名单'
            return $false
        }
        60011 {
            Write-Err "$Label 无权限调用（errcode=60011），确认已开通会话内容存档"
            return $false
        }
        default {
            Write-Err "$Label gettoken 失败：errcode=$($resp.errcode) errmsg=$($resp.errmsg)"
            return $false
        }
    }
}

function Invoke-Verify {
    Write-Step '凭据与文件检查'
    $failed = 0

    if (-not (Test-Path $EnvFile)) {
        Write-Err '.env 不存在'; $failed++
    } else {
        foreach ($k in $RequiredKeys) {
            if ([string]::IsNullOrWhiteSpace((Get-EnvValue $k))) { Write-Err "$k 未配置"; $failed++ }
            else { Write-Ok "$k 已配置" }
        }
        if ([string]::IsNullOrWhiteSpace((Get-EnvValue 'EXTERNAL_CONTACT_SECRET'))) {
            Write-Warn2 'EXTERNAL_CONTACT_SECRET 未配置（仅影响外部客户群资料）'
        } else { Write-Ok 'EXTERNAL_CONTACT_SECRET 已配置' }
    }

    if (Test-Path $PrivPath) { Write-Ok "私钥存在: $PrivPath" } else { Write-Err '私有密钥缺失（先跑 -GenKeys）'; $failed++ }

    if (Test-Path $SoPath) {
        $elf = Test-ElfX64 $SoPath
        if ($elf.Ok) { Write-Ok "SDK 存在且架构正确 ($($elf.Reason))" }
        else { Write-Err "SDK 无效：$($elf.Reason)"; $failed++ }
    } else {
        Write-Err "SDK 缺失（先跑 -InstallSdk -SdkPath <文件>）"; $failed++
    }

    if (Test-Path $PrivPath) {
        Write-Step '私钥强度检查'
        $code = 'from Crypto.PublicKey import RSA; print(RSA.import_key(open(''/app/secrets/private_key.pem'',''rb'').read()).size_in_bits())'
        $bits = (Invoke-InContainer $code | Select-Object -Last 1)
        if ($bits -match '2048|4096') { Write-Ok "私钥位数 $bits" }
        else { Write-Warn2 "私钥位数异常：$bits（企业微信要求 2048）" }
    }

    if ($failed -eq 0) {
        Write-Step '开放接口连通性'
        $corpId = Get-EnvValue 'CORP_ID'
        $secret = Get-EnvValue 'MSGAUDIT_SECRET'
        $tokenOk = Test-GetToken $corpId $secret '会话内容存档'
        $extSecret = Get-EnvValue 'EXTERNAL_CONTACT_SECRET'
        if ($extSecret) { Test-GetToken $corpId $extSecret '客户联系' | Out-Null }
        if (-not $tokenOk) { $failed++ }
    }

    if ($ForceRecreate) {
        Write-Step '重建容器使配置生效'
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            docker compose up -d --force-recreate 2>&1 | Out-Null
            Start-Sleep -Seconds 5
            $status = (docker compose ps --format '{{.Status}}' 2>&1 | Out-String).Trim()
        } finally {
            $ErrorActionPreference = $prev
        }
        if ($status -match 'healthy') { Write-Ok "容器状态: $status" } else { Write-Warn2 "容器状态: $status（健康检查可能需要更多时间）" }
    }

    if ($SmokePull -and $failed -eq 0) {
        Write-Step '真实拉取 1 条消息（端到端验证）'
        $code = 'from wecom_core import WeComConfig, pull_messages; c=WeComConfig.from_env(); r=pull_messages(c, limit=1, save_to_file=False); print(''success=%d fail=%d seq=%d->%d'' % (r.success_count, r.fail_count, r.start_seq, r.end_seq)); [print(''ERR'', e) for e in r.errors]'
        $out = Invoke-InContainer $code
        Write-Host $out
        if ($out -match 'success=1') { Write-Ok '拉取并解密成功，改一个 sdkfileid 即可验证媒体下载' }
        elseif ($out -match 'success=0') { Write-Warn2 '拉取成功但无新消息：seq 已到最新，或用 wecom_set_seq 回退后再试' }
        else { Write-Err '端到端拉取失败，见上方输出' }
    }

    Write-Step '结论'
    if ($failed -eq 0) {
        Write-Ok '全部前置条件就绪'
        Write-Host '  管理后台仍需确认：存档范围已覆盖目标成员、员工已同意存档' -ForegroundColor Yellow
    } else {
        Write-Err "还有 $failed 项未就绪"
    }
    return $failed
}

# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
if (-not ($GenKeys -or $InstallSdk -or $SetCredential -or $Console -or $Verify)) {
    Write-Host '企业微信会话存档部署助手  用法示例:' -ForegroundColor Cyan
    Write-Host '  .\setup-wecom.ps1 -GenKeys'
    Write-Host '  .\setup-wecom.ps1 -InstallSdk -SdkPath .\WeWorkFinanceSdk_C_Linux.tar.gz'
    Write-Host '  .\setup-wecom.ps1 -Console            # 复制公钥 + 探测出口 IP + 打开后台'
    Write-Host '  .\setup-wecom.ps1 -SetCredential'
    Write-Host '  .\setup-wecom.ps1 -Verify -ForceRecreate -SmokePull'
    exit 0
}

if ($GenKeys) { New-RsaKeyPair }
if ($InstallSdk) { Install-Sdk }
if ($Console) { Show-ConsoleAssist }
if ($SetCredential) { Set-Credential }
if ($Verify) {
    $remain = Invoke-Verify
    if ($remain -gt 0) { exit 1 }
}
