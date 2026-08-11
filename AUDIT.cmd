@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\check.ps1"
if errorlevel 1 (
  echo Полный аудит завершился с ошибкой.
  echo Отчёт сохранён в runtime\audit\latest.txt
  pause
  exit /b 1
)
echo.
echo Report saved to runtime\audit\latest.txt
pause
exit /b 0
