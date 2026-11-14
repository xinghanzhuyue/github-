@echo off
rem Push helper for agents. ASCII only on purpose: cmd.exe mangles UTF-8 batch files.
rem Usage:  push-github.cmd [args passed to scripts\push-github.ps1]
setlocal
set "SCRIPT=%~dp0push-github.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
exit /b %ERRORLEVEL%
