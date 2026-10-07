@echo off
rem 一键更新 hosts 加速条目（自动请求管理员权限）
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-admin.ps1" -Action hosts
