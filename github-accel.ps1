#Requires -Version 5.1
<#
.SYNOPSIS
    GitHub 访问加速脚本（Windows / PowerShell 5.1+）

.DESCRIPTION
    针对国内访问 GitHub 常见的四类问题，提供诊断与修复：
      1) DNS 污染        -> Action hosts   （GitHub520 多源自动更新 hosts，可回滚）
      2) 缺少代理        -> Action proxy   （自动探测本地代理端口并写入 git 全局配置）
      3) 下载慢/被墙     -> Action mirror  （镜像 URL 重写 + 命令行下载加速）
      4) 不知道问题在哪  -> Action diagnose（DNS / TCP / TLS 分层体检并给出结论）

.PARAMETER Action
    status    查看当前加速状态（默认）
    diagnose  网络分层诊断，输出问题定位与建议
    hosts     更新 hosts 加速条目（需要管理员权限）
    proxy     配置 git 代理（自动探测端口，或 -ProxyUrl 指定）
    mirror    镜像 URL 重写（只读加速；push 不可用，开启期间勿输入任何令牌）
    download  通过镜像下载 GitHub 文件 / release，配合 -DownloadUrl
    task      注册 / 卸载"每天自动更新 hosts"的计划任务（需要管理员权限）
    all       一键启用：hosts + proxy（能自动探测到才配）
    off       一键还原：移除 hosts 条目 + 取消 git 代理 + 移除镜像重写
    help      显示用法

.PARAMETER DryRun
    只显示将要做的改动，不实际写入（hosts / proxy / mirror / task 均支持）。

.PARAMETER Restore
    hosts 专用：从最近一次备份恢复 hosts（等价于撤销上一次 hosts 更新）。

.PARAMETER Off
    proxy / mirror 专用：关闭对应加速。

.PARAMETER Uninstall
    task 专用：删除计划任务。

.PARAMETER ProxyUrl
    手动指定代理地址，例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080。

.PARAMETER Port
    只探测指定端口（默认探测内置常见端口列表 + 系统代理端口）。

.PARAMETER DownloadUrl
    要下载的 GitHub 链接（release / raw / codeload 均可）。

.PARAMETER OutFile
    download 的保存路径，默认取链接文件名并放到当前目录。

.PARAMETER Mirror
    强制指定镜像前缀，例如 https://ghproxy.net/

.PARAMETER Yes
    跳过交互确认。

.EXAMPLE
    .\github-accel.ps1 -Action diagnose
    先定位问题类型。

.EXAMPLE
    # 以管理员身份运行 PowerShell 后：
    .\github-accel.ps1 -Action hosts
    更新 hosts 加速条目。

.EXAMPLE
    .\github-accel.ps1 -Action hosts -DryRun
    预览会写入哪些条目，不修改系统。

.EXAMPLE
    .\github-accel.ps1 -Action proxy
    自动探测本地代理端口并配置 git。

.EXAMPLE
    .\github-accel.ps1 -Action download -DownloadUrl "https://github.com/git/git/archive/refs/heads/master.tar.gz"

.EXAMPLE
    .\github-accel.ps1 -Action off
    一键还原全部改动。
#>
[CmdletBinding()]
param(
    [ValidateSet('status', 'diagnose', 'hosts', 'proxy', 'mirror', 'download', 'task', 'all', 'off', 'help')]
    [string]$Action = 'status',

    [switch]$DryRun,
    [switch]$Restore,
    [switch]$Off,
    [switch]$Uninstall,

    [string]$ProxyUrl,
    [int]$Port = 0,
    [string]$DownloadUrl,
    [string]$OutFile,
    [string]$Mirror,

    [switch]$Yes,
    [string]$TaskName = 'GitHubAccel-HostsUpdate',
    [string]$TaskTime = '12:30'
)

$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------- 全局常量 ---

if ($PSScriptRoot) { $Script:Root = $PSScriptRoot } else { $Script:Root = (Get-Location).Path }
$Script:BackupDir = Join-Path $Script:Root 'backup'
$Script:LogDir = Join-Path $Script:Root 'logs'
$Script:LogFile = Join-Path $Script:LogDir ('accel-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))
$Script:HostsPath = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'

$Script:MarkerStart = '# >>> github-accel >>>'
$Script:MarkerEnd = '# <<< github-accel <<<'

# hosts 数据源，多源回退（按顺序尝试）
$Script:HostsSources = @(
    'https://raw.hellogithub.com/hosts',
    'https://cdn.jsdelivr.net/gh/521xueweihan/GitHub520@main/hosts',
    'https://ghproxy.net/https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts',
    'https://ghfast.top/https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts',
    'https://hub.gitmirror.com/https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts'
)

# 允许写入 hosts 的域名（防止数据源被篡改后污染其它域名）
$Script:DomainPattern = '^(?:[a-z0-9_-]+\.)*(?:github\.com|githubusercontent\.com|githubassets\.com|github\.io|githubapp\.com|github\.dev|ghcr\.io)$|^github\.global\.ssl\.fastly\.net$'

# 诊断用的关键域名（第一个是基础连通性，其余是常见加速目标）
$Script:PublicDns = '223.5.5.5'

$Script:ProbeDomains = @(
    'github.com',
    'api.github.com',
    'raw.githubusercontent.com',
    'codeload.github.com',
    'objects.githubusercontent.com',
    'gist.githubusercontent.com',
    'github.githubassets.com',
    'avatars.githubusercontent.com'
)

# 常见本地代理端口
$Script:ProxyPorts = @(7890, 7891, 7897, 7898, 7899, 1080, 1081, 10808, 10809, 2080, 2081, 8080, 8118, 8889, 1087, 20171, 33210)

# 镜像前缀（ghproxy 系的用法：镜像前缀 + 原始 GitHub 链接）
$Script:Mirrors = @(
    'https://ghproxy.net/',
    'https://ghfast.top/',
    'https://gh-proxy.com/',
    'https://hub.gitmirror.com/',
    'https://ghproxy.cc/'
)

# 我们写入的 git 配置项，方便一键撤销
$Script:GitKeys = @('http.proxy', 'https.proxy', 'http.version', 'http.postBuffer', 'http.lowSpeedLimit', 'http.lowSpeedTime')

$Script:Changed = New-Object System.Collections.Generic.List[string]

# ---------------------------------------------------------------- 输出工具 ---

function Initialize-Dirs {
    foreach ($d in @($Script:BackupDir, $Script:LogDir)) {
        if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    try {
        Initialize-Dirs
        $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
        Add-Content -Path $Script:LogFile -Value $line -Encoding UTF8
    } catch { }
}

function Write-Info {
    param([string]$Message)
    Write-Host "  $Message"
    Write-Log $Message
}

function Write-Ok {
    param([string]$Message)
    Write-Host "  [OK] $Message" -ForegroundColor Green
    Write-Log $Message 'OK'
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  [警告] $Message" -ForegroundColor Yellow
    Write-Log $Message 'WARN'
}

function Write-Err {
    param([string]$Message)
    Write-Host "  [错误] $Message" -ForegroundColor Red
    Write-Log $Message 'ERROR'
}

function Write-Step {
    param([string]$Message)
    Write-Host "  -> $Message" -ForegroundColor DarkGray
    Write-Log $Message 'STEP'
}

function Write-Title {
    param([string]$Message)
    Write-Host ''
    Write-Host "===== $Message =====" -ForegroundColor Cyan
    Write-Log "===== $Message ====="
}

function Write-Usage {
    Write-Host @'
GitHub 访问加速脚本  github-accel.ps1

用法：
  .\github-accel.ps1 -Action <动作> [选项]

动作：
  status                    查看当前加速状态（默认）
  diagnose                  分层诊断：DNS 污染 / TCP 阻断 / TLS 干扰 / 代理缺失
  hosts                     更新 hosts 加速条目（需管理员）
  proxy                     配置 git 代理（自动探测本地端口）
  mirror                    镜像 URL 重写（只读加速；push 不可用，开启期间勿输入任何令牌）
  download                  通过镜像下载 GitHub 文件 / release
  task                      注册或卸载"每天自动更新 hosts"计划任务（需管理员）
  all                       一键启用 hosts + proxy
  off                       一键还原全部改动
  help                      显示本帮助

常用选项：
  -DryRun                   预览改动，不写入
  -Restore                  hosts：从最近备份恢复
  -Off                      proxy / mirror：关闭
  -Uninstall                task：删除计划任务
  -ProxyUrl <url>           指定代理，如 http://127.0.0.1:7890
  -Port <n>                 只探测指定代理端口
  -DownloadUrl <url>        download 的目标链接
  -OutFile <path>           download 的保存路径
  -Mirror <prefix>          强制指定镜像前缀，如 https://ghproxy.net/
  -Yes                      跳过确认

示例：
  .\github-accel.ps1 -Action diagnose
  .\github-accel.ps1 -Action hosts -DryRun
  .\github-accel.ps1 -Action hosts          # 管理员
  .\github-accel.ps1 -Action proxy
  .\github-accel.ps1 -Action download -DownloadUrl "https://github.com/git/git/archive/refs/heads/master.tar.gz"
  .\github-accel.ps1 -Action off
'@
}

# ------------------------------------------------------------ 基础环境探测 ---

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Get-GitExe {
    $cmd = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command git -ErrorAction SilentlyContinue }
    if ($cmd) { return $cmd.Source }
    foreach ($p in @(
            (Join-Path $env:ProgramFiles 'Git\cmd\git.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Git\cmd\git.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd\git.exe'))) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

function Get-CurlExe {
    $cmd = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# 统一的 HTTP 取文本：先 curl，再 Invoke-WebRequest，失败返回 $null
function Invoke-HttpText {
    param([string]$Url, [int]$TimeoutSec = 20)

    $curl = Get-CurlExe
    if ($curl) {
        try {
            $out = & $curl -sSL --max-time $TimeoutSec --compressed $Url 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { return ($out -join "`n") }
        } catch { }
    }
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        if ($resp.StatusCode -eq 200 -and $resp.Content) { return $resp.Content }
    } catch { }
    return $null
}

function Test-TcpPort {
    param([string]$HostName, [int]$Port = 443, [int]$TimeoutMs = 3000)

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $client = New-Object System.Net.Sockets.TcpClient
    $result = [pscustomobject]@{ Ok = $false; Ms = 0; Error = '' }
    try {
        $iar = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) { throw 'TCP 连接超时' }
        $client.EndConnect($iar)
        $result.Ok = $true
    } catch {
        $result.Error = $_.Exception.Message
    } finally {
        $sw.Stop()
        $result.Ms = [int]$sw.ElapsedMilliseconds
        try { $client.Close() } catch { }
    }
    return $result
}

function Test-TlsHandshake {
    param([string]$HostName, [int]$Port = 443, [int]$TimeoutMs = 5000)

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $client = New-Object System.Net.Sockets.TcpClient
    $result = [pscustomobject]@{ Ok = $false; Ms = 0; Protocol = ''; Subject = ''; Error = '' }
    try {
        $iar = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) { throw 'TCP 连接超时' }
        $client.EndConnect($iar)
        $stream = $client.GetStream()
        $callback = [System.Net.Security.RemoteCertificateValidationCallback] { param($s, $c, $ch, $e) return $true }
        $ssl = New-Object System.Net.Security.SslStream($stream, $false, $callback)
        $ssl.AuthenticateAsClient($HostName)
        $result.Ok = $true
        $result.Protocol = [string]$ssl.SslProtocol
        try { $result.Subject = $ssl.RemoteCertificate.Subject } catch { }
        $ssl.Dispose()
    } catch {
        $result.Error = $_.Exception.Message
    } finally {
        $sw.Stop()
        $result.Ms = [int]$sw.ElapsedMilliseconds
        try { $client.Close() } catch { }
    }
    return $result
}

# ---------------------------------------------------------------- DNS / hosts ---

function Resolve-GitHubDomain {
    param([string]$Domain, [string]$Server)

    $result = [pscustomobject]@{ Domain = $Domain; Server = $Server; IPs = @(); State = 'NO_RESULT' }
    try {
        if ($Server) {
            $recs = Resolve-DnsName -Name $Domain -Type A -Server $Server -DnsOnly -ErrorAction Stop
        } else {
            $recs = Resolve-DnsName -Name $Domain -Type A -DnsOnly -ErrorAction Stop
        }
        # 只取应答段（Answer）的 A 记录，排除权威段/附加段里的 NS 主机地址
        $ips = @($recs | Where-Object { $_.Type -eq 'A' -and $_.Section -ne 'Additional' } | ForEach-Object { $_.IPAddress })
        $result.IPs = $ips
        if ($ips.Count -eq 0) { $result.State = 'NO_RESULT' }
        elseif ($ips | Where-Object { $_ -eq '0.0.0.0' -or $_ -eq '127.0.0.1' -or $_ -eq '::' }) { $result.State = 'BLACKHOLE' }
        else { $result.State = 'OK' }
    } catch {
        $result.State = 'FAIL'
    }
    return $result
}

function ConvertTo-HostsEntries {
    param([string]$Text)

    $map = [ordered]@{}
    foreach ($raw in ($Text -split "`r?`n")) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $line = ($line -split '#')[0].Trim()
        $parts = @($line -split '\s+' | Where-Object { $_ })
        if ($parts.Count -lt 2) { continue }

        $parsed = [System.Net.IPAddress]::Any
        if (-not [System.Net.IPAddress]::TryParse($parts[0], [ref]$parsed)) { continue }
        if ($parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { continue }

        for ($i = 1; $i -lt $parts.Count; $i++) {
            $d = $parts[$i].ToLower()
            if ($d -notmatch $Script:DomainPattern) { continue }
            if (-not $map.Contains($d)) { $map[$d] = $parts[0] }
        }
    }
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($k in $map.Keys) { $out.Add(("{0}`t{1}" -f $map[$k], $k)) }
    return $out
}

function Get-RemoteHostsEntries {
    foreach ($src in $Script:HostsSources) {
        Write-Step "尝试数据源：$src"
        $text = Invoke-HttpText -Url $src -TimeoutSec 20
        if (-not $text) { Write-Warn '拉取失败，换下一个源'; continue }
        $entries = ConvertTo-HostsEntries -Text $text
        if ($entries.Count -lt 5) { Write-Warn "解析出的条目太少（$($entries.Count) 条），视为无效源"; continue }
        Write-Ok "数据源可用：$src（$($entries.Count) 条域名）"
        return [pscustomobject]@{ Source = $src; Entries = $entries }
    }
    return $null
}

function Get-HostsBlockEntries {
    if (-not (Test-Path $Script:HostsPath)) { return @() }
    $text = [System.IO.File]::ReadAllText($Script:HostsPath)
    $pattern = [regex]::Escape($Script:MarkerStart) + '(?s)(.*?)' + [regex]::Escape($Script:MarkerEnd)
    $m = [regex]::Match($text, $pattern)
    if (-not $m.Success) { return @() }
    return @(ConvertTo-HostsEntries -Text $m.Groups[1].Value)
}

function Backup-File {
    param([string]$Path, [string]$Tag)

    Initialize-Dirs
    $name = '{0}.{1}.bak' -f $Tag, (Get-Date -Format 'yyyyMMdd-HHmmss')
    $dest = Join-Path $Script:BackupDir $name
    Copy-Item -Path $Path -Destination $dest -Force
    Write-Info "已备份：$dest"
    return $dest
}

function Set-HostsBlock {
    param([string[]]$Lines, [switch]$Remove)

    $text = [System.IO.File]::ReadAllText($Script:HostsPath)
    $pattern = [regex]::Escape($Script:MarkerStart) + '(?s).*?' + [regex]::Escape($Script:MarkerEnd)

    if ($Remove) {
        $new = [regex]::Replace($text, $pattern, '')
        $new = [regex]::Replace($new, '(\r?\n){3,}', "`r`n`r`n").TrimEnd() + "`r`n"
    } else {
        $block = $Script:MarkerStart + "`r`n" +
                 "# Generated by github-accel.ps1 at " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " - do not edit this block" + "`r`n" +
                 ($Lines -join "`r`n") + "`r`n" +
                 $Script:MarkerEnd
        if ([regex]::IsMatch($text, $pattern)) {
            $new = [regex]::Replace($text, $pattern, $block)
        } else {
            $new = $text.TrimEnd() + "`r`n`r`n" + $block + "`r`n"
        }
    }
    [System.IO.File]::WriteAllText($Script:HostsPath, $new, (New-Object System.Text.UTF8Encoding($false)))
}

function Update-DnsCache {
    try { & ipconfig /flushdns | Out-Null; Write-Info 'DNS 缓存已刷新 (ipconfig /flushdns)' } catch { Write-Warn 'DNS 缓存刷新失败，可手动执行 ipconfig /flushdns' }
}

function Invoke-HostsAction {
    param([switch]$RemoveOnly, [switch]$RestoreFromBackup)

    Write-Title 'hosts 加速'

    if (-not (Test-Admin)) {
        Write-Err 'hosts 文件需要管理员权限，请以管理员身份重新打开 PowerShell / 终端后重试。'
        Write-Info '提示：Win + X -> 「终端(管理员)」或「Windows PowerShell(管理员)」'
        return
    }

    if ($RemoveOnly) {
        $existing = @(Get-HostsBlockEntries)
        if ($existing.Count -eq 0) { Write-Info 'hosts 中没有本脚本生成的条目，无需处理。'; return }
        if ($DryRun) { Write-Info "[DryRun] 将从 hosts 移除 $($existing.Count) 条加速条目"; return }
        Backup-File -Path $Script:HostsPath -Tag 'hosts' | Out-Null
        Set-HostsBlock -Remove
        Update-DnsCache
        Write-Ok "已从 hosts 移除 $($existing.Count) 条加速条目。"
        $Script:Changed.Add('移除 hosts 加速条目')
        return
    }

    if ($RestoreFromBackup) {
        $baks = @(Get-ChildItem -Path $Script:BackupDir -Filter 'hosts.*.bak' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
        if ($baks.Count -eq 0) {
            Write-Warn '没有找到备份，改为仅移除自动生成的条目。'
            if (-not $DryRun) { Backup-File -Path $Script:HostsPath -Tag 'hosts' | Out-Null; Set-HostsBlock -Remove }
            Write-Ok '已移除 github-accel 生成的 hosts 条目。'
            if (-not $DryRun) { Update-DnsCache }
            return
        }
        Write-Info "使用备份：$($baks[0].FullName)"
        if ($DryRun) { Write-Info '[DryRun] 将用该备份覆盖 hosts'; return }
        Copy-Item -Path $baks[0].FullName -Destination $Script:HostsPath -Force
        Update-DnsCache
        Write-Ok 'hosts 已从备份恢复。'
        $Script:Changed.Add('hosts 从备份恢复')
        return
    }

    $fetched = Get-RemoteHostsEntries
    if (-not $fetched) {
        Write-Err '所有数据源都拉取失败。'
        Write-Info '可选做法：先跑 -Action proxy 让网络通，或改用 -Action download 手动获取 hosts 文件。'
        return
    }

    $lines = @($fetched.Entries)
    Write-Info '将写入以下条目：'
    foreach ($l in $lines) { Write-Host ("    " + $l) -ForegroundColor DarkGray }

    if ($DryRun) {
        Write-Info '[DryRun] 未修改 hosts 文件。'
        return
    }

    Backup-File -Path $Script:HostsPath -Tag 'hosts' | Out-Null
    Set-HostsBlock -Lines $lines
    Update-DnsCache

    Write-Info '验证解析结果：'
    $expected = @{}
    foreach ($l in $lines) {
        $p = $l -split "`t"
        if ($p.Count -ge 2 -and -not $expected.ContainsKey($p[1])) { $expected[$p[1]] = $p[0] }
    }
    $bad = 0
    foreach ($d in @('github.com', 'raw.githubusercontent.com', 'codeload.github.com')) {
        $r = Resolve-GitHubDomain -Domain $d
        if ($r.State -ne 'OK') {
            Write-Warn "$d 解析状态异常：$($r.State)"
            $bad++
        } elseif (-not $expected.ContainsKey($d)) {
            Write-Info "$d -> $($r.IPs -join ', ')（本次数据源未提供该域名）"
        } elseif ($r.IPs -contains $expected[$d]) {
            Write-Ok "$d -> $($r.IPs -join ', ')"
        } else {
            Write-Warn "$d -> $($r.IPs -join ', ')（与写入值 $($expected[$d]) 不一致，可能被上游缓存覆盖）"
        }
    }

    Write-Ok 'hosts 更新完成。'
    $Script:Changed.Add("hosts 更新（来源：$($fetched.Source)，$($lines.Count) 条）")
    if ($bad -gt 0) {
        Write-Warn '部分域名解析仍异常：如果 TLS 层被干扰，hosts 无法解决，请改用 -Action proxy（本地代理）。'
    }
}

# ------------------------------------------------------------------- git 代理 ---

function Get-LocalProxyCandidates {
    $found = New-Object System.Collections.Generic.List[object]

    $ports = @()
    if ($Port -gt 0) { $ports = @($Port) } else { $ports = $Script:ProxyPorts }

    foreach ($p in $ports) {
        $r = Test-TcpPort -HostName '127.0.0.1' -Port $p -TimeoutMs 400
        if ($r.Ok) { $found.Add([pscustomobject]@{ Url = "http://127.0.0.1:$p"; Port = $p; Source = '端口探测' }) }
    }

    try {
        $reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
        if ($reg -and $reg.ProxyServer) {
            $srv = [string]$reg.ProxyServer
            if ($srv -match '^(?:https?=)?([^;]+)') {
                $hp = $Matches[1]
                if ($hp -notmatch '^https?://') { $hp = "http://$hp" }
                if (-not ($found | Where-Object { $_.Url -eq $hp })) {
                    $found.Add([pscustomobject]@{ Url = $hp; Port = 0; Source = '系统代理设置' })
                }
            }
        }
    } catch { }

    return $found
}

function Get-GitConfigValue {
    param([string]$Git, [string]$Key)
    $v = & $Git config --global --get $Key 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($v | Select-Object -First 1)
}

function Set-GitConfig {
    param([string]$Git, [string]$Key, [string]$Value)
    if ($DryRun) { Write-Info "[DryRun] git config --global $Key `"$Value`""; return }
    & $Git config --global $Key $Value | Out-Null
}

function Invoke-ProxyAction {
    param([switch]$Disable, [string]$Proxy)

    Write-Title 'git 代理配置'

    $git = Get-GitExe
    if (-not $git) {
        Write-Err '未检测到 git。'
        Write-Info '可安装：winget install --id Git.Git -e --source winget'
        Write-Info '安装后重新打开终端，再执行 -Action proxy。'
    }

    if ($Disable) {
        if (-not $git) { return }
        foreach ($k in $Script:GitKeys) {
            if ($DryRun) { Write-Info "[DryRun] 取消 $k"; continue }
            & $git config --global --unset-all $k 2>$null | Out-Null
        }
        Write-Ok 'git 代理相关配置已取消。'
        $Script:Changed.Add('取消 git 代理')
        return
    }

    $url = $Proxy
    if (-not $url) {
        $cands = Get-LocalProxyCandidates
        if ($cands.Count -eq 0) {
            Write-Warn '没有探测到本地代理端口。'
            Write-Info "已探测端口：$($Script:ProxyPorts -join ', ')"
            Write-Info '如果代理软件用的是其它端口，请用 -ProxyUrl http://127.0.0.1:端口 指定。'
            if (-not $git) { return }
        } else {
            Write-Info '探测到的代理候选：'
            foreach ($c in $cands) { Write-Host ("    {0}   ({1})" -f $c.Url, $c.Source) -ForegroundColor DarkGray }
            $url = $cands[0].Url
        }
    }

    if (-not $url) { return }
    if ($url -notmatch '^[a-z0-9]+://') { $url = "http://$url" }
    Write-Info "使用代理：$url"

    if (-not $git) { return }

    if ($DryRun) {
        Write-Info '[DryRun] 未修改 git 配置。'
    } else {
        try {
            Initialize-Dirs
            $snap = Join-Path $Script:BackupDir ('gitconfig.{0}.bak' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
            & $git config --global --list 2>$null | Set-Content -Path $snap -Encoding UTF8
            Write-Info "已备份 git 全局配置：$snap"
        } catch { Write-Warn '备份 git 配置失败（不影响继续）。' }
    }

    Set-GitConfig -Git $git -Key 'http.proxy' -Value $url
    Set-GitConfig -Git $git -Key 'https.proxy' -Value $url
    # 代理场景下 HTTP/2 容易出现卡死
    Set-GitConfig -Git $git -Key 'http.version' -Value 'HTTP/1.1'
    Set-GitConfig -Git $git -Key 'http.postBuffer' -Value '524288000'
    Set-GitConfig -Git $git -Key 'http.lowSpeedLimit' -Value '1000'
    Set-GitConfig -Git $git -Key 'http.lowSpeedTime' -Value '60'

    if ($DryRun) { Write-Info '[DryRun] 未修改 git 配置。'; return }

    Write-Ok 'git 代理已配置完成。'
    Write-Info '验证：git ls-remote https://github.com/git/git HEAD'
    Write-Info '撤销：.\github-accel.ps1 -Action proxy -Off'
    $Script:Changed.Add("git 代理 -> $url")
}

# --------------------------------------------------------------------- 镜像 ---

function Test-MirrorSpeed {
    param([string]$Prefix, [int]$TimeoutSec = 12)

    $probe = 'https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts'
    $url = $Prefix + $probe
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $text = Invoke-HttpText -Url $url -TimeoutSec $TimeoutSec
    $sw.Stop()
    if ($text -and $text.Length -gt 50) {
        return [pscustomobject]@{ Prefix = $Prefix; Ok = $true; Ms = [int]$sw.ElapsedMilliseconds }
    }
    return [pscustomobject]@{ Prefix = $Prefix; Ok = $false; Ms = [int]$sw.ElapsedMilliseconds }
}

function Select-Mirror {
    param([string]$Forced)

    if ($Forced) {
        if ($Forced -notmatch '/$') { $Forced = "$Forced/" }
        Write-Info "使用指定镜像：$Forced"
        return $Forced
    }
    Write-Info '测试各镜像可用性...'
    $ok = @()
    foreach ($m in $Script:Mirrors) {
        $r = Test-MirrorSpeed -Prefix $m
        if ($r.Ok) { Write-Ok ("{0}  可用 ({1} ms)" -f $r.Prefix, $r.Ms); $ok += $r }
        else { Write-Warn ("{0}  不可用" -f $r.Prefix) }
    }
    if ($ok.Count -eq 0) { return $null }
    return ($ok | Sort-Object Ms | Select-Object -First 1).Prefix
}

function Invoke-MirrorAction {
    param([switch]$Disable, [string]$MirrorPrefix)

    Write-Title '镜像 URL 重写'

    $git = Get-GitExe
    if (-not $git) {
        Write-Err '未检测到 git，无法写入重写规则。'
        Write-Info '可安装：winget install --id Git.Git -e --source winget'
        return
    }

    if ($Disable) {
        $removed = 0
        foreach ($m in $Script:Mirrors) {
            $key = 'url.' + $m + 'https://github.com/.insteadOf'
            if ($DryRun) { Write-Info "[DryRun] 取消 $key"; continue }
            & $git config --global --unset-all $key 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $removed++ }
        }
        Write-Ok "镜像重写规则已清理（移除 $removed 条）。"
        $Script:Changed.Add('清理镜像重写')
        return
    }

    $prefix = Select-Mirror -Forced $MirrorPrefix
    if (-not $prefix) {
        Write-Err '所有镜像都不可用，暂不写入重写规则。'
        return
    }

    $key = 'url.' + $prefix + 'https://github.com/.insteadOf'
    if ($DryRun) {
        Write-Info "[DryRun] git config --global `"$key`" `"https://github.com/`""
        return
    }
    & $git config --global $key 'https://github.com/' | Out-Null

    Write-Ok "已启用镜像：$prefix"
    Write-Warn '注意：镜像只适合 clone / fetch / 下载，push 会失败。'
    Write-Warn '需要 push 的仓库请临时执行 -Action mirror -Off，或直接用代理方案。'
    Write-Warn '⚠ 安全提醒：镜像状态下的 git 流量（含你输入的账号 / 令牌）会先经过第三方镜像服务器。'
    Write-Warn '⚠  开启期间不要输入任何令牌（PAT / 密码）；要 push 或登录，先执行 -Action mirror -Off。'
    Write-Info '撤销：.\github-accel.ps1 -Action mirror -Off'
    $Script:Changed.Add("镜像重写 -> $prefix")
}

function Invoke-DownloadAction {
    Write-Title '镜像下载'

    if (-not $DownloadUrl) {
        Write-Err '请用 -DownloadUrl 指定要下载的 GitHub 链接。'
        return
    }

    $prefix = Select-Mirror -Forced $Mirror
    if (-not $prefix) { Write-Err '所有镜像都不可用，无法下载。'; return }

    $url = $DownloadUrl
    if ($url -match '^https://(github\.com|raw\.githubusercontent\.com|codeload\.github\.com|objects\.githubusercontent\.com|gist\.githubusercontent\.com)/') {
        $url = $prefix + $url
    } else {
        Write-Warn '链接不是 GitHub 域名，将按原样下载。'
    }

    if (-not $OutFile) {
        $name = ($DownloadUrl -split '\?')[0].TrimEnd('/').Split('/')[-1]
        if (-not $name) { $name = 'download.bin' }
        $OutFile = Join-Path (Get-Location).Path $name
    }

    Write-Info "下载：$url"
    Write-Info "保存：$OutFile"
    if ($DryRun) { Write-Info '[DryRun] 未实际下载。'; return }

    $curl = Get-CurlExe
    if ($curl) {
        & $curl -L --fail --retry 3 --retry-delay 2 -o $OutFile $url
        if ($LASTEXITCODE -eq 0) { Write-Ok "下载完成：$OutFile"; $Script:Changed.Add("下载 $OutFile") }
        else { Write-Err "下载失败（curl 退出码 $LASTEXITCODE）。可换 -Mirror 指定其它镜像重试。" }
    } else {
        try {
            Invoke-WebRequest -Uri $url -OutFile $OutFile -UseBasicParsing -TimeoutSec 120
            Write-Ok "下载完成：$OutFile"
            $Script:Changed.Add("下载 $OutFile")
        } catch { Write-Err "下载失败：$($_.Exception.Message)" }
    }
}

# --------------------------------------------------------------------- 诊断 ---

function Invoke-DiagnoseAction {
    Write-Title '环境'
    $admin = Test-Admin
    if ($admin) { Write-Ok '当前是管理员权限（可写 hosts / 注册计划任务）' } else { Write-Warn '当前不是管理员权限（hosts 更新与计划任务需要管理员）' }
    Write-Info "PowerShell 版本：$($PSVersionTable.PSVersion)"
    Write-Info "操作系统：$([Environment]::OSVersion.VersionString)"

    $git = Get-GitExe
    if ($git) { Write-Ok "git：$git" } else { Write-Warn '未检测到 git（clone/代理配置需要它，可 winget install --id Git.Git -e）' }
    if (Get-CurlExe) { Write-Ok 'curl.exe 可用' } else { Write-Warn 'curl.exe 不可用（下载加速会退回 PowerShell 实现）' }

    Write-Title '系统代理'
    try {
        $reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
        if ($reg.ProxyEnable -eq 1) { Write-Ok "系统代理已开启：$($reg.ProxyServer)" } else { Write-Info '系统代理未开启' }
        if ($reg.AutoConfigURL) { Write-Info "PAC：$($reg.AutoConfigURL)" }
    } catch { }
    $cands = Get-LocalProxyCandidates
    if ($cands.Count -gt 0) {
        foreach ($c in $cands) { Write-Ok "本地代理可用：$($c.Url)（$($c.Source)）" }
    } else {
        Write-Warn "未在本机探测到常见代理端口（$($Script:ProxyPorts -join ', ')）"
    }

    Write-Title 'DNS 解析'
    Write-Info "对比用公共 DNS：$($Script:PublicDns)"
    $dnsBad = @()
    $dnsDiff = @()
    foreach ($d in $Script:ProbeDomains) {
        $r = Resolve-GitHubDomain -Domain $d
        $pub = Resolve-GitHubDomain -Domain $d -Server $Script:PublicDns
        $localIps = ($r.IPs -join ', ')
        $pubIps = ($pub.IPs -join ', ')
        switch ($r.State) {
            'OK' {
                $note = ''
                if ($pub.State -eq 'OK' -and $pub.IPs.Count -gt 0 -and -not ($r.IPs | Where-Object { $pub.IPs -contains $_ })) {
                    $note = "  (与 $($Script:PublicDns) 结果不同：$pubIps)"
                    $dnsDiff += $d
                }
                Write-Host ("  [OK]   {0,-32} -> {1}{2}" -f $d, $localIps, $note) -ForegroundColor Green
            }
            'BLACKHOLE' {
                Write-Host ("  [污染] {0,-32} -> {1}  (黑洞地址；$($Script:PublicDns) 返回：$pubIps)" -f $d, $localIps) -ForegroundColor Red
                $dnsBad += $d
            }
            'NO_RESULT' {
                Write-Host ("  [无解析] {0,-30} 本地无 A 记录；$($Script:PublicDns) 返回：$pubIps" -f $d) -ForegroundColor Red
                $dnsBad += $d
            }
            default {
                Write-Host ("  [失败] {0,-32} 解析失败；$($Script:PublicDns) 返回：$pubIps" -f $d) -ForegroundColor Red
                $dnsBad += $d
            }
        }
    }

    Write-Title 'TCP / TLS 连通性'
    $tlsBad = @()
    foreach ($d in @('github.com', 'raw.githubusercontent.com', 'codeload.github.com')) {
        $tcp = Test-TcpPort -HostName $d -Port 443 -TimeoutMs 4000
        if (-not $tcp.Ok) {
            $msg = $tcp.Error
            if ($msg -match '找不到请求的类型的数据|No such host|no data') { $msg = '域名解析不到地址（DNS 污染 / 黑洞）' }
            elseif ($msg -match '超时|timed out') { $msg = '连接超时（IP 被阻断或丢包严重）' }
            elseif ($msg -match '拒绝|refused') { $msg = '连接被拒绝（端口被封 / RST 干扰）' }
            Write-Host ("  [TCP失败] {0,-30} {1}" -f $d, $msg) -ForegroundColor Red
            continue
        }
        $tls = Test-TlsHandshake -HostName $d -Port 443 -TimeoutMs 6000
        if ($tls.Ok) {
            Write-Host ("  [OK]      {0,-30} TCP {1} ms, TLS {2} {3} ms" -f $d, $tcp.Ms, $tls.Protocol, $tls.Ms) -ForegroundColor Green
        } elseif ($tls.Error -match 'No credentials are available|SEC_E_NO_CREDENTIALS') {
            Write-Host ("  [跳过]    {0,-30} TCP {1} ms 通了；TLS 无法测试（当前进程环境限制，非网络问题）" -f $d, $tcp.Ms) -ForegroundColor Yellow
        } else {
            Write-Host ("  [TLS失败] {0,-30} TCP {1} ms 通了，但 TLS 握手失败：{2}" -f $d, $tcp.Ms, $tls.Error) -ForegroundColor Red
            $tlsBad += $d
        }
    }
    if ($tlsBad.Count -gt 0) {
        Write-Info '说明：若在受限/沙箱环境运行，TLS 失败可能是环境限制而非网络问题，请用普通 PowerShell 窗口复测。'
    }

    Write-Title '结论与建议'
    $advice = New-Object System.Collections.Generic.List[string]
    if ($dnsBad.Count -gt 0) {
        $advice.Add("DNS 异常域名 $($dnsBad.Count) 个（$($dnsBad -join ', ')）-> 建议执行：.\github-accel.ps1 -Action hosts（管理员）")
    }
    if ($dnsDiff.Count -gt 0) {
        $advice.Add("本地 DNS 与公共 DNS 结果不一致（$($dnsDiff -join ', ')）-> 可先把网卡 DNS 改成 $($Script:PublicDns) 再测；不行就用 hosts 方案。")
    }
    if ($tlsBad.Count -gt 0) {
        $advice.Add("TLS 握手被干扰（$($tlsBad -join ', ')）-> hosts 无法解决，建议用本地代理：.\github-accel.ps1 -Action proxy")
    }
    if ($cands.Count -eq 0 -and $tlsBad.Count -gt 0) {
        $advice.Add('本机没有可用代理，但 TLS 被干扰：需要先准备一个可用的本地代理软件（Clash / v2ray 等）再执行 proxy。')
    }
    if ($dnsBad.Count -eq 0 -and $tlsBad.Count -eq 0) {
        $advice.Add('DNS 与 TLS 均正常；若仍感觉慢，属跨境带宽问题 -> 可试 .\github-accel.ps1 -Action proxy 或 -Action mirror。')
    }
    if (-not $git) {
        $advice.Add('未安装 git：winget install --id Git.Git -e --source winget')
    }
    if ($advice.Count -eq 0) { $advice.Add('没有发现明显问题。') }
    $i = 1
    foreach ($a in $advice) { Write-Host "  $i) $a" -ForegroundColor Yellow; $i++ }
}

# --------------------------------------------------------------------- 状态 ---

function Invoke-StatusAction {
    Write-Title '当前状态'

    $entries = @(Get-HostsBlockEntries)
    if ($entries.Count -gt 0) { Write-Ok "hosts 加速条目：$($entries.Count) 条（由本脚本管理）" }
    else { Write-Info 'hosts 加速条目：未启用' }

    if (-not (Test-Admin)) { Write-Info '管理员权限：否（更新 hosts / 计划任务需要）' }

    $git = Get-GitExe
    if ($git) {
        $p = Get-GitConfigValue -Git $git -Key 'http.proxy'
        if ($p) { Write-Ok "git 代理：$p" } else { Write-Info 'git 代理：未设置' }
        $rules = @(& $git config --global --get-regexp '^url\..*\.insteadOf$' 2>$null)
        if ($rules.Count -gt 0) {
            Write-Ok "镜像重写：$($rules.Count) 条"
            foreach ($r in $rules) { Write-Host "    $r" -ForegroundColor DarkGray }
        } else { Write-Info '镜像重写：未启用' }
    } else {
        Write-Warn '未检测到 git'
    }

    $cands = Get-LocalProxyCandidates
    if ($cands.Count -gt 0) { Write-Info "本地代理候选：$(($cands | ForEach-Object { $_.Url }) -join ', ')" }
    else { Write-Info '本地代理候选：无' }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Write-Ok "计划任务：$TaskName（$($task.State)）" } else { Write-Info "计划任务：未注册" }

    Write-Info "日志文件：$Script:LogFile"
    $baks = @(Get-ChildItem -Path $Script:BackupDir -Filter '*.bak' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 3)
    if ($baks.Count -gt 0) {
        Write-Info '最近备份：'
        foreach ($b in $baks) { Write-Host ("    {0}  ({1})" -f $b.Name, $b.LastWriteTime) -ForegroundColor DarkGray }
    }
}

# ----------------------------------------------------------------- 计划任务 ---

function Invoke-TaskAction {
    Write-Title '计划任务'

    if (-not (Test-Admin)) { Write-Err '注册 / 删除计划任务需要管理员权限。'; return }

    if ($Uninstall -or $Off) {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $t) { Write-Info "计划任务 $TaskName 不存在。"; return }
        if ($DryRun) { Write-Info "[DryRun] 将删除计划任务 $TaskName"; return }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "已删除计划任务：$TaskName"
        $Script:Changed.Add('删除计划任务')
        return
    }

    $scriptPath = Join-Path $Script:Root 'github-accel.ps1'
    if (-not (Test-Path $scriptPath)) { Write-Err "找不到脚本：$scriptPath"; return }

    $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Action hosts -Yes' -f $scriptPath
    if ($DryRun) {
        Write-Info "[DryRun] 将注册计划任务：$TaskName，每天 $TaskTime 运行 hosts 更新"
        Write-Info "         动作：powershell.exe $arg"
        return
    }

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
    $trigger = New-ScheduledTaskTrigger -Daily -At $TaskTime
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -RunLevel Highest -LogonType Interactive
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Write-Ok "已注册计划任务：$TaskName（每天 $TaskTime 自动更新 hosts）"
    Write-Info "删除：.\github-accel.ps1 -Action task -Uninstall"
    $Script:Changed.Add("注册计划任务 $TaskName")
}

# ---------------------------------------------------------------- 汇总与入口 ---

function Show-Summary {
    if ($Script:Changed.Count -eq 0) { return }
    Write-Title '本次改动'
    foreach ($c in $Script:Changed) { Write-Host "  - $c" -ForegroundColor Green }
    Write-Info "日志：$Script:LogFile"
    $baks = @(Get-ChildItem -Path $Script:BackupDir -Filter '*.bak' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
    if ($baks.Count -gt 0) { Write-Info "最近备份：$($baks[0].FullName)" }
    Write-Info '一键还原：.\github-accel.ps1 -Action off'
}

Initialize-Dirs
Write-Log "启动：Action=$Action DryRun=$DryRun Off=$Off Restore=$Restore"

switch ($Action) {
    'help' { Write-Usage }
    'status' { Invoke-StatusAction }
    'diagnose' { Invoke-DiagnoseAction }
    'hosts' { Invoke-HostsAction -RestoreFromBackup:$Restore }
    'proxy' { Invoke-ProxyAction -Proxy $ProxyUrl -Disable:$Off }
    'mirror' { Invoke-MirrorAction -MirrorPrefix $Mirror -Disable:$Off }
    'download' { Invoke-DownloadAction }
    'task' { Invoke-TaskAction }
    'all' {
        Invoke-HostsAction
        Invoke-ProxyAction -Proxy $ProxyUrl
        Invoke-StatusAction
    }
    'off' {
        Write-Title '一键还原'
        Invoke-HostsAction -RemoveOnly
        Invoke-ProxyAction -Disable
        Invoke-MirrorAction -Disable
        Invoke-StatusAction
    }
}

Show-Summary
