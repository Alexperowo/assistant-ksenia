@echo off
chcp 65001 >nul
start "" notepad.exe "%~dp0docs\MODEL-SEARCH-REQUEST-RU.md"
if errorlevel 1 exit /b 1
exit /b 0
