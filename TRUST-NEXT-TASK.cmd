@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" trust-next-task
if errorlevel 1 (
  echo Не удалось изменить режим доверенной задачи.
  pause
  exit /b 1
)
pause
exit /b 0
