@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ============================================================
echo   Открытие порта 8765 для Ксении в Брандмауэре Windows
echo ============================================================
echo.
echo Сейчас появится стандартный запрос Windows на подтверждение прав администратора.
echo Пожалуйста, нажмите "Да".
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"\"%~dp0scripts\configure-firewall.ps1\"\"' -Wait"
echo.
echo Настройка завершена.
pause
