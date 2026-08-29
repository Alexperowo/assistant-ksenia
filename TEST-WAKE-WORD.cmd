@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" wake-test
set "exitCode=%errorlevel%"
pause
exit /b %exitCode%
