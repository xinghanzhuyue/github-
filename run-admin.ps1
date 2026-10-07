#Requires -Version 5.1
<#
.SYNOPSIS
    以管理员身份运行 github-accel.ps1 的启动器（会弹出 UAC 授权框）。

.DESCRIPTION
    更新系统 hosts 文件需要管理员权限，但"以管理员身份打开终端再 cd 过去"很麻烦。
    本启动器自动以管理员身份重新拉起 github-accel.ps1，并保留窗口方便看结果。

.PARAMETER Action
    传给 github-accel.ps1 的动作，默认 hosts（更新 hosts 加速条目）。
    例如：-Action all、-Action off、-Action task -Uninstall

.PARAMETER Extra
    追加传给 github-accel.ps1 的其它参数，例如 -Extra '-DryRun'
#>
[CmdletBinding()]
param(
    [string]$Action = 'hosts',
    [string]$Extra
)

$ErrorActionPreference = 'Stop'

$accel = Join-Path $PSScriptRoot 'github-accel.ps1'
if (-not (Test-Path $accel)) {
    Write-Host "找不到 github-accel.ps1：$accel" -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}

$argList = @(
    '-NoExit',
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', "`"$accel`"",
    '-Action', $Action
)
if ($Extra) { $argList += ($Extra -split '\s+') }

Write-Host "正在以管理员身份启动：$Action ..." -ForegroundColor Cyan
try {
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList
} catch {
    Write-Host "提权失败或被取消：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '也可以手动以管理员身份打开 PowerShell，然后执行：' -ForegroundColor Yellow
    Write-Host "  & `"$accel`" -Action $Action" -ForegroundColor Yellow
    Read-Host '按回车退出'
    exit 1
}
