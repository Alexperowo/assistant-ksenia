@echo off
setlocal
chcp 65001 >nul
if "%~1"=="" (
  echo Перетащите ZIP-архив Ксении на этот файл или передайте путь первым параметром.
  pause
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\verify-release.ps1" -ArchivePath "%~1"
if errorlevel 1 (
  echo Архив не прошёл проверку.
  pause
  exit /b 1
)
pause
