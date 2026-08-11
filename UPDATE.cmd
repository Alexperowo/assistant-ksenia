@echo off
setlocal
chcp 65001 >nul
echo Безопасное обновление Ксении до версий, одобренных текущим релизом.
echo Личные настройки, память, проекты и модели сохраняются.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update.ps1" %*
if errorlevel 1 (
  echo Обновление не завершено. Подробности находятся в runtime\updates.
  pause
  exit /b 1
)
pause
