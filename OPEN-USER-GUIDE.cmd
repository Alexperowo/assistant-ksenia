@echo off
chcp 65001 >nul
start "" notepad.exe "%~dp0docs\ИНСТРУКЦИЯ-ПОЛЬЗОВАТЕЛЯ.txt"
if errorlevel 1 exit /b 1
exit /b 0
