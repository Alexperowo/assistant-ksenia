@echo off
setlocal
chcp 65001 >nul
echo Создание чистого архива Ксении без моделей и личных данных.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-release.ps1" %*
if errorlevel 1 (
  echo Архив не создан. Исходный проект не изменён.
  pause
  exit /b 1
)
pause
