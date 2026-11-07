#Requires -Version 5.1
<#
.SYNOPSIS
    双击「启动界面.cmd」时跑到这里：找到可用的 Python，以无终端方式打开图形界面。

.DESCRIPTION
    探测顺序：py 启动器（取它背后的真实 python.exe）-> PATH 里的 python.exe
    （跳过 Microsoft Store 占位程序）。找到后优先用同目录的 pythonw.exe 启动
    github_gui.pyw（不出现终端窗口）；找不到 Python 时给出两条明确的出路
    （装 Python，或改用免 Python 的 PowerShell 版）。

    注意：-3 是 py 启动器的参数，python.exe / pythonw.exe 本身不认识，
    所以解析到真实路径后直接启动，不再转发任何启动器参数。
#>
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$root = $PSScriptRoot
$script = Join-Path $root 'github_gui.pyw'
if (-not (Test-Path -LiteralPath $script)) {
    Write-Host "找不到 github_gui.pyw：$script" -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}

function Get-RealPython {
    # 返回可用的 python.exe 完整路径；找不到返回 $null。
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $exe = & $pyCmd.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
    $pyExe = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pyExe -and $pyExe.Source -notmatch 'WindowsApps') {
        # WindowsApps 里的是 Microsoft Store 占位程序，只会把人带去商店
        & $pyExe.Source -c "import tkinter" 2>$null
        if ($LASTEXITCODE -eq 0) { return $pyExe.Source }
    }
    return $null
}

$pyExe = Get-RealPython
if (-not $pyExe) {
    Write-Host ''
    Write-Host '没有检测到可用的 Python，图形界面暂时打不开。两条路任选：' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  方案 A（不装任何东西）：双击「一键更新hosts.cmd」，' -ForegroundColor White
    Write-Host '           用 PowerShell 版更新 hosts 加速条目，同样能解决 DNS 污染。' -ForegroundColor White
    Write-Host '  方案 B（想要图形界面）：先安装 Python 3.8 以上版本 ——' -ForegroundColor White
    Write-Host '           到 https://www.python.org/downloads/ 下载，或在微软商店搜「Python 3.12」，' -ForegroundColor White
    Write-Host '           安装时务必勾选「Add python.exe to PATH」，装完重新双击「启动界面.cmd」。' -ForegroundColor White
    Write-Host ''
    Read-Host '按回车退出'
    exit 1
}

Write-Host "使用 Python：$pyExe" -ForegroundColor Gray

# 优先 pythonw.exe：不弹终端窗口
$pyw = Join-Path (Split-Path -Parent $pyExe) 'pythonw.exe'
if (Test-Path -LiteralPath $pyw) {
    Start-Process -FilePath $pyw -ArgumentList "`"$script`"" -WorkingDirectory $root
    exit 0
}

# 没有 pythonw（精简发行版）就退回带终端的启动方式
& $pyExe $script
exit $LASTEXITCODE
