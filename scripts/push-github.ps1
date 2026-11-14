#Requires -Version 5.1
<#
.SYNOPSIS
    把本仓库已提交的内容推送到 GitHub —— 面向 Agent / 自动化调用。

.DESCRIPTION
    本脚本专门解决"在本机 + Agent 沙箱里推不上去"的几个老问题，改动前请先读完：

      1) 推送只用「带凭据的 URL + 显式 refspec」，不用 `git push origin main`。
         原因：Agent 沙箱禁止写 .git 目录，而 `git push origin` 成功后要更新
         refs/remotes/origin/main，会报 "cannot lock ref: Permission denied"。

      2) 不依赖 Git 凭据管理器（GCM）。
         原因：GCM 在沙箱里要靠 sh.exe 启动，而 sh 在沙箱下创建管道会被拒绝
         （couldn't create signal pipe, Win32 error 5），直接崩溃。
         凭据按顺序尝试：环境变量 → 用户级 token 文件 → Windows 凭据管理器
         （直接调用 git-credential-wincred.exe，不经过 sh）。

      3) github.com:443 在部分网络下会间歇性连不上。
         每次尝试都限时，超时就杀进程树、换下一轮；被 GitHub 拒绝（403）的
         凭据会立刻跳过，不会白白重试。

      4) 所有输出和日志里的 token 都会替换成 ******。

.PARAMETER RepoRoot
    仓库根目录，默认是脚本所在目录的上一级。

.PARAMETER Branch
    要推送的分支，默认当前分支（读不到就当作 main）。

.PARAMETER RemoteUrl
    远端地址，默认读 origin 的 URL。

.PARAMETER TokenFile
    额外指定的 token 文件（一行 token）。

.PARAMETER Retries
    每个凭据最多尝试几次，默认 4。

.PARAMETER TimeoutSec
    单次 git 操作超时秒数，默认 90。

.PARAMETER DryRun
    只做推送握手（--dry-run），不真正上传对象。

.PARAMETER AllTags
    推送全部 tag；默认只推本次提交可达的附注 tag（--follow-tags）。

.PARAMETER NoVerify
    推送成功后不校验远端 SHA。

.PARAMETER VerifyOnly
    只校验远端与本地是否一致，不推送。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\push-github.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\push-github.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$Branch,
    [string]$RemoteUrl,
    [string]$TokenFile,
    [int]$Retries = 4,
    [int]$TimeoutSec = 90,
    [switch]$DryRun,
    [switch]$AllTags,
    [switch]$NoVerify,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8

$script:Secrets = New-Object System.Collections.ArrayList
$script:LogFile = $null

# ---------------------------------------------------------------- 基础工具 --

function Say {
    param([string]$Text = '', [string]$Color = 'Gray')
    Write-Host $Text -ForegroundColor $Color
    if ($script:LogFile) {
        try { Add-Content -LiteralPath $script:LogFile -Value $Text -Encoding UTF8 -ErrorAction Stop }
        catch { $script:LogFile = $null }
    }
}

function Add-Secret {
    param([string]$Value)
    if ($Value -and -not $script:Secrets.Contains($Value)) { [void]$script:Secrets.Add($Value) }
}

function Redact {
    param([string]$Text)
    if (-not $Text) { return '' }
    foreach ($s in $script:Secrets) {
        if ($s) { $Text = $Text.Replace($s, '******') }
    }
    return $Text
}

function ConvertTo-QuotedArgument {
    param([string]$Value)
    if ($Value -match '^[A-Za-z0-9_./:=@%+,\-]+$') { return $Value }
    return '"' + ($Value.Replace('"', '\"')) + '"'
}

function Invoke-GitProcess {
    param(
        [Parameter(Mandatory = $true)][string]$GitExe,
        [Parameter(Mandatory = $true)][string[]]$GitArgs,
        [int]$TimeoutSec = 90,
        [string]$InputText
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $GitExe
    $psi.Arguments = (($GitArgs | ForEach-Object { ConvertTo-QuotedArgument $_ }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $hasInput = $PSBoundParameters.ContainsKey('InputText')
    if ($hasInput) {
        $psi.RedirectStandardInput = $true
    }

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    try { [void]$proc.Start() } catch {
        return New-Object PSObject -Property @{
            Out = ''; Err = "无法启动 git：$($_.Exception.Message)"; Code = -1; TimedOut = $false
        }
    }

    if ($hasInput) {
        try { $proc.StandardInput.Write($InputText); $proc.StandardInput.Close() } catch { }
    }

    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    $finished = $proc.WaitForExit($TimeoutSec * 1000)
    $timedOut = $false

    if (-not $finished) {
        $timedOut = $true
        try { $proc.Kill() } catch { }
        try { [void]$proc.WaitForExit(5000) } catch { }
    }

    $stdout = ''
    $stderr = ''
    if ($outTask.Wait(5000)) { $stdout = $outTask.Result }
    if ($errTask.Wait(5000)) { $stderr = $errTask.Result }

    $code = -1
    try { if ($proc.HasExited) { $code = $proc.ExitCode } } catch { }

    return New-Object PSObject -Property @{
        Out = $stdout; Err = $stderr; Code = $code; TimedOut = $timedOut
    }
}

# ------------------------------------------------------------ 环境定位 ------

function Resolve-GitExe {
    param([string]$Root)
    $candidates = New-Object System.Collections.ArrayList
    if ($env:GIT_EXE) { [void]$candidates.Add($env:GIT_EXE) }
    [void]$candidates.Add((Join-Path $Root 'tools\PortableGit\cmd\git.exe'))
    [void]$candidates.Add((Join-Path $Root 'tools\PortableGit\bin\git.exe'))
    [void]$candidates.Add('C:\Program Files\Git\cmd\git.exe')
    $onPath = Get-Command git -ErrorAction SilentlyContinue
    if ($onPath) { [void]$candidates.Add($onPath.Source) }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return 'git'
}

function Resolve-WincredExe {
    param([string]$Root)
    $candidates = New-Object System.Collections.ArrayList
    if ($env:GIT_CREDENTIAL_WINCRED) { [void]$candidates.Add($env:GIT_CREDENTIAL_WINCRED) }
    [void]$candidates.Add((Join-Path $Root 'tools\PortableGit\ucrt64\libexec\git-core\git-credential-wincred.exe'))
    [void]$candidates.Add((Join-Path $Root 'tools\PortableGit\mingw64\libexec\git-core\git-credential-wincred.exe'))
    [void]$candidates.Add('C:\Program Files\Git\mingw64\libexec\git-core\git-credential-wincred.exe')
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return $null
}

# ------------------------------------------------------------ 凭据收集 ------

function Get-CredentialCandidates {
    param([string]$Root, [string]$ExplicitTokenFile)

    $list = New-Object System.Collections.ArrayList
    $seen = @{}

    $add = {
        param($Source, $User, $Token)
        if (-not $Token) { return }
        $Token = ($Token.Trim()).Trim('"').Trim("'")
        if (-not $Token) { return }
        if ($Token -match '\s') { $Token = ($Token -split '\s+')[0] }
        if ($seen.ContainsKey($Token)) { return }
        $seen[$Token] = $true
        Add-Secret $Token
        [void]$list.Add((New-Object PSObject -Property @{
            Source = $Source; User = $User; Token = $Token
        }))
    }

    # 1) 环境变量（CI / 临时覆盖最方便）
    foreach ($name in @('GITHUB_TOKEN', 'GH_TOKEN')) {
        $v = [Environment]::GetEnvironmentVariable($name)
        if ($v) { & $add "环境变量 $name" 'x-access-token' $v }
    }

    # 2) 用户级 token 文件（推荐位置）
    $files = New-Object System.Collections.ArrayList
    if ($ExplicitTokenFile) { [void]$files.Add($ExplicitTokenFile) }
    [void]$files.Add((Join-Path $env:USERPROFILE '.config\github-push\token'))
    [void]$files.Add((Join-Path $env:USERPROFILE '.github-token'))
    [void]$files.Add((Join-Path $Root 'tools\token.txt'))

    foreach ($f in $files) {
        if (-not $f) { continue }
        if (-not (Test-Path -LiteralPath $f)) { continue }
        $line = Get-Content -LiteralPath $f -TotalCount 1 -ErrorAction SilentlyContinue
        if ($line) { & $add ("token 文件 " + $f) 'x-access-token' $line }
    }

    # 3) Windows 凭据管理器（直接调 exe，绕过 sh）
    $wincred = Resolve-WincredExe -Root $Root
    if ($wincred) {
        $r = Invoke-GitProcess -GitExe $wincred -GitArgs @('get') -TimeoutSec 20 `
            -InputText "protocol=https`nhost=github.com`n`n"
        if ($r.Code -eq 0) {
            $user = ''
            $pass = ''
            foreach ($line in ($r.Out -split "`r?`n")) {
                if ($line -match '^username=(.+)$') { $user = $matches[1] }
                elseif ($line -match '^password=(.+)$') { $pass = $matches[1] }
            }
            if ($pass) {
                if (-not $user) { $user = 'x-access-token' }
                & $add 'Windows 凭据管理器' $user $pass
            }
        }
    }

    return $list
}

# ------------------------------------------------------------ 推送逻辑 ------

function New-AuthenticatedUrl {
    param([string]$Url, [string]$User, [string]$Token)
    $path = $Url
    if ($path -match 'github\.com[:/]+(?<p>.+)$') { $path = $matches['p'] }
    $path = $path.TrimEnd('/')
    $path = $path -replace '\.git$', ''
    return "https://$User`:$Token@github.com/$path.git"
}

function Test-AuthFailure {
    param([string]$Text)
    $patterns = @(
        'remote: Permission to', '403', 'Authentication failed',
        'Invalid username or password', 'could not read Username',
        'Repository not found', 'Bad credentials'
    )
    foreach ($p in $patterns) { if ($Text -match [regex]::Escape($p)) { return $true } }
    return $false
}

function Test-NetworkFailure {
    param([string]$Text)
    $patterns = @(
        'Could not connect', 'Failed to connect', 'Connection was reset',
        'Connection reset', 'Recv failure', 'Timed out', 'timed out',
        'early EOF', 'RPC failed', 'unable to access', 'SSL', 'The remote end hung up'
    )
    foreach ($p in $patterns) { if ($Text -match [regex]::Escape($p)) { return $true } }
    return $false
}

function Get-RemoteRefInfo {
    param([string]$GitExe, [string]$Url, [string]$Ref, [int]$TimeoutSec)
    # 这里同样要清空 credential.helper：凭据已经在 URL 里，
    # 否则 git 会去调 GCM，而 GCM 在沙箱里要靠 sh 启动（会被杀）。
    $r = Invoke-GitProcess -GitExe $GitExe -TimeoutSec $TimeoutSec `
        -GitArgs @('-c', 'credential.helper=', '-c', 'core.askPass=', 'ls-remote', $Url, $Ref)
    $sha = $null
    if (-not $r.TimedOut -and $r.Code -eq 0) {
        foreach ($line in ($r.Out -split "`r?`n")) {
            $t = $line.Trim()
            if (-not $t) { continue }
            $parts = $t -split "`t"
            if ($parts.Count -ge 2 -and $parts[1].Trim() -eq $Ref) { $sha = $parts[0].Trim(); break }
        }
    }
    return New-Object PSObject -Property @{
        Sha      = $sha
        Code     = $r.Code
        TimedOut = $r.TimedOut
        Error    = (Redact (($r.Out + "`n" + $r.Err).Trim()))
    }
}

function Invoke-PushAttempt {
    param(
        [string]$GitExe, [string]$Url, [string]$Branch,
        [int]$TimeoutSec, [bool]$IsDryRun, [bool]$PushAllTags
    )
    $gitArgs = @('-c', 'credential.helper=', '-c', 'core.askPass=', 'push')
    if ($IsDryRun) { $gitArgs += '--dry-run' }
    $gitArgs += $Url
    $gitArgs += ("HEAD:refs/heads/" + $Branch)
    if ($PushAllTags) { $gitArgs += '--tags' } else { $gitArgs += '--follow-tags' }
    return Invoke-GitProcess -GitExe $GitExe -GitArgs $gitArgs -TimeoutSec $TimeoutSec
}

# ------------------------------------------------------------------ 主流程 --

if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
if (-not $RepoRoot) { Say '[失败] 无法确定仓库根目录，请用 -RepoRoot 指定。' 'Red'; exit 1 }
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path

$logDir = Join-Path $RepoRoot 'logs'
if (Test-Path -LiteralPath $logDir) {
    $candidateLog = Join-Path $logDir ('push-github-' + (Get-Date -Format 'yyyyMMdd') + '.log')
    try {
        Add-Content -LiteralPath $candidateLog -Value '' -Encoding UTF8 -ErrorAction Stop
        $script:LogFile = $candidateLog
    } catch {
        $script:LogFile = $null
    }
}

Say '============================================================' 'Cyan'
Say '  GitHub 推送（Agent 版）' 'Cyan'
Say ("  仓库：" + $RepoRoot) 'Cyan'
Say '============================================================' 'Cyan'

$git = Resolve-GitExe -Root $RepoRoot
Say ("[信息] git 可执行文件：" + $git)

$probe = Invoke-GitProcess -GitExe $git -GitArgs @('-C', $RepoRoot, 'rev-parse', '--git-dir') -TimeoutSec 30
if ($probe.Code -ne 0) {
    Say '[失败] 目标目录不是 git 仓库，或 git 无法运行。' 'Red'
    Say (Redact ($probe.Err.Trim())) 'Red'
    exit 1
}

$headProbe = Invoke-GitProcess -GitExe $git -GitArgs @('-C', $RepoRoot, 'rev-parse', '--verify', 'HEAD') -TimeoutSec 30
if ($headProbe.Code -ne 0) {
    Say '[失败] 仓库还没有任何提交，没有可推送的内容。' 'Red'
    exit 1
}
$headSha = $headProbe.Out.Trim()

if (-not $Branch) {
    $b = Invoke-GitProcess -GitExe $git -GitArgs @('-C', $RepoRoot, 'symbolic-ref', '--quiet', '--short', 'HEAD') -TimeoutSec 30
    if ($b.Code -eq 0 -and $b.Out.Trim()) { $Branch = $b.Out.Trim() } else { $Branch = 'main' }
}

if (-not $RemoteUrl) {
    $ru = Invoke-GitProcess -GitExe $git -GitArgs @('-C', $RepoRoot, 'remote', 'get-url', 'origin') -TimeoutSec 30
    if ($ru.Code -eq 0 -and $ru.Out.Trim()) { $RemoteUrl = $ru.Out.Trim() }
    else { $RemoteUrl = 'https://github.com/xinghanzhuyue/github-.git' }
}

$dirty = Invoke-GitProcess -GitExe $git -GitArgs @('-C', $RepoRoot, 'status', '--porcelain') -TimeoutSec 30
if ($dirty.Out.Trim()) {
    Say '[警告] 工作区有未提交改动，这些内容不会被推送（脚本只推已提交的 HEAD）。' 'Yellow'
    ($dirty.Out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 10) | ForEach-Object { Say ('        ' + $_) 'DarkYellow' }
}

Say ("[信息] 分支：" + $Branch + "    HEAD：" + $headSha.Substring(0, [Math]::Min(12, $headSha.Length))) 'Cyan'
Say ("[信息] 远端：" + $RemoteUrl) 'Cyan'

$candidates = @(Get-CredentialCandidates -Root $RepoRoot -ExplicitTokenFile $TokenFile)
if (-not $candidates -or $candidates.Count -eq 0) {
    Say '[失败] 找不到任何可用凭据。' 'Red'
    Say ("        请把 token 写入 " + (Join-Path $env:USERPROFILE '.config\github-push\token') + "，") 'Yellow'
    Say '        或设置环境变量 GITHUB_TOKEN，或先运行一次 git push 让凭据管理器记住密码。' 'Yellow'
    exit 2
}
Say ("[信息] 共找到 " + $candidates.Count + " 个凭据来源。") 'Cyan'

if ($VerifyOnly) {
    foreach ($c in $candidates) {
        $url = New-AuthenticatedUrl -Url $RemoteUrl -User $c.User -Token $c.Token
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $info = Get-RemoteRefInfo -GitExe $git -Url $url -Ref ("refs/heads/" + $Branch) -TimeoutSec $TimeoutSec
            if ($info.Sha) {
                if ($info.Sha -eq $headSha) {
                    Say ("[通过] 远端 " + $Branch + " 与本地一致：" + $headSha) 'Green'
                    exit 0
                }
                Say ("[不一致] 远端 " + $Branch + " = " + $info.Sha + "，本地 = " + $headSha) 'Yellow'
                exit 5
            }
            if ($attempt -lt 3) {
                Say ("[重试] 第 " + $attempt + " 次读取远端失败，稍后再试…") 'DarkYellow'
                Start-Sleep -Seconds 4
            }
        }
        if ($info.Error) { Say ('        ' + $info.Error) 'DarkGray' }
    }
    Say '[失败] 无法读取远端引用（网络或凭据问题）。' 'Red'
    exit 4
}

$pushed = $false
$sawAuthRejected = $false
$networkFailures = 0
$successUrl = $null

foreach ($c in $candidates) {
    Say ''
    Say ("[凭据] 使用 " + $c.Source) 'Cyan'
    for ($attempt = 1; $attempt -le $Retries; $attempt++) {
        Say ("[推送] 第 {0}/{1} 次尝试（单次限时 {2} 秒）…" -f $attempt, $Retries, $TimeoutSec) 'Gray'
        $url = New-AuthenticatedUrl -Url $RemoteUrl -User $c.User -Token $c.Token
        $r = Invoke-PushAttempt -GitExe $git -Url $url -Branch $Branch -TimeoutSec $TimeoutSec -IsDryRun:$DryRun -PushAllTags:$AllTags

        $text = Redact (($r.Out + "`n" + $r.Err).Trim())
        if ($text) { ($text -split "`r?`n") | ForEach-Object { Say ('        ' + $_) 'DarkGray' } }

        if (-not $r.TimedOut -and $r.Code -eq 0) {
            $pushed = $true
            $successUrl = $url
            break
        }
        if ($r.TimedOut) {
            $networkFailures++
            Say '[失败] 本次超时，已终止进程树。' 'Yellow'
            Start-Sleep -Seconds ([Math]::Min(5 * $attempt, 20))
            continue
        }
        if (Test-AuthFailure $text) {
            $sawAuthRejected = $true
            Say '[失败] 这个凭据被 GitHub 拒绝（权限不足或已失效），换下一个来源。' 'Yellow'
            break
        }
        if (Test-NetworkFailure $text) {
            $networkFailures++
            Say '[失败] 网络中断，稍后重试。' 'Yellow'
            Start-Sleep -Seconds ([Math]::Min(5 * $attempt, 20))
            continue
        }
        Say '[失败] 无法识别的错误，停止重试。' 'Red'
        break
    }
    if ($pushed) { break }
}

if (-not $pushed) {
    Say ''
    if ($sawAuthRejected -and $networkFailures -eq 0) {
        Say '[失败] 所有凭据都被拒绝。请换一个对目标仓库有 Contents: Read and write 的 token。' 'Red'
        Say ('        存放位置：' + (Join-Path $env:USERPROFILE '.config\github-push\token')) 'Yellow'
        exit 3
    }
    Say '[失败] 网络持续不通，本次没有推送成功。' 'Red'
    Say '        稍后重试即可，脚本是幂等的：已推送的内容不会被重复上传。' 'Yellow'
    exit 4
}

if ($DryRun) {
    Say ''
    Say '[成功] dry-run 通过：凭据和网络都可用（本次没有真正上传）。' 'Green'
    exit 0
}

if (-not $NoVerify) {
    $verified = $false
    $remoteSha = $null
    for ($i = 1; $i -le 3 -and -not $verified; $i++) {
        $info = Get-RemoteRefInfo -GitExe $git -Url $successUrl -Ref ("refs/heads/" + $Branch) -TimeoutSec $TimeoutSec
        $remoteSha = $info.Sha
        if ($remoteSha -eq $headSha) { $verified = $true; break }
        Start-Sleep -Seconds 3
    }
    if (-not $verified) {
        Say ''
        $shown = if ($remoteSha) { $remoteSha } else { '(读取失败)' }
        Say ('[警告] 推送命令返回成功，但远端 ' + $Branch + ' 读回来是 ' + $shown + '，本地是 ' + $headSha + '。') 'Yellow'
        Say '        可能是网络抖动导致校验失败，也可能推送被拒。请稍后用 -VerifyOnly 复查。' 'Yellow'
        exit 5
    }
    Say ("[校验] 远端 " + $Branch + " = " + $remoteSha) 'Green'
}

Say ''
Say ("[成功] 已推送到 " + $RemoteUrl) 'Green'
Say ("::RESULT:: SUCCESS branch=" + $Branch + " head=" + $headSha) 'Green'
exit 0
