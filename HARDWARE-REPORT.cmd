@echo off
setlocal
chcp 65001 >nul
echo Ксения проверит оборудование без изменения настроек.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\hardware-report.ps1" %*
if errorlevel 1 (
  echo Не удалось составить отчёт об оборудовании.
  pause
  exit /b 1
)
pause
