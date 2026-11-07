@echo off
rem ---------------------------------------------------------------
rem  One-click: launch the GUI console (github_gui.pyw).
rem
rem  Keep this file pure ASCII on purpose (same as the hosts cmd):
rem  cmd.exe parses .cmd files with the console code page, so UTF-8
rem  Chinese would scramble the script - that is also why the ps1
rem  below has an ASCII file name. Chinese messages live in
rem  start-gui.ps1 (UTF-8 with BOM), which PowerShell reads fine.
rem ---------------------------------------------------------------
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-gui.ps1"
if errorlevel 1 pause
