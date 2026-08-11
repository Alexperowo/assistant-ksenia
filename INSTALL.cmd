@echo off
setlocal
chcp 65001 >nul
echo Проверка и восстановление локальной среды Ксении.
echo Существующий Python будет сохранён. Загрузка нужна только если Python отсутствует.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-runtime.ps1" %*
if errorlevel 1 (
  echo Установка завершилась с ошибкой. Окно оставлено открытым.
  pause
  exit /b 1
)
echo Готово. Ярлыки созданы на рабочем столе.
pause
