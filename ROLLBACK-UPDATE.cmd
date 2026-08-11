@echo off
setlocal
chcp 65001 >nul
echo Возврат последнего обновления Ксении из сохранённой резервной копии.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\rollback-update.ps1" %*
if errorlevel 1 (
  echo Откат не завершён. Исходные резервные копии сохранены.
  pause
  exit /b 1
)
pause
