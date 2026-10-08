@echo off
rem ---------------------------------------------------------------
rem  One-click: update the GitHub hosts entries (asks for admin).
rem
rem  Keep this file pure ASCII on purpose: cmd.exe parses .cmd files
rem  with the console code page (936/GBK on Chinese Windows), so a
rem  UTF-8 Chinese character can swallow the line break and scramble
rem  the rest of the script. All Chinese messages are printed by
rem  run-admin.ps1 (UTF-8 with BOM), which PowerShell reads fine.
rem ---------------------------------------------------------------
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-admin.ps1" -Action hosts
if errorlevel 1 pause
