$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$kseniaIcon = Join-Path $projectRoot 'assets\icons\ksenia.ico'
$shortcuts = @(
    @{ Name = 'Ксения — инструкция пользователя'; Target = 'OPEN-USER-GUIDE.cmd'; Description = 'Пошаговая инструкция пользователя по проверке и работе с Ксенией.' },
    @{ Name = 'Ксения — запрос для поиска моделей'; Target = 'OPEN-MODEL-SEARCH-REQUEST.cmd'; Description = 'Открыть готовое сообщение для поиска Dense MTP и MoE APEX MTP моделей.' },
    @{ Name = 'Ксения — НАЧАТЬ РАЗГОВОР'; Target = 'START-VOICE.cmd'; Hotkey = 'CTRL+ALT+K'; Description = 'Главный голосовой режим. Горячая клавиша Control Alt K.' },
    @{ Name = 'Ксения — ОСТАНОВИТЬ ГОЛОС'; Target = 'STOP-VOICE.cmd'; Hotkey = 'CTRL+ALT+S'; Description = 'Аварийно остановить только голосовой режим. Горячая клавиша Control Alt S.' },
    @{ Name = 'Ксения — помощь и управление'; Target = 'START-BUTLER.cmd'; Hotkey = 'CTRL+ALT+U'; Description = 'Меню состояния, моделей и настроек. Горячая клавиша Control Alt U.' },
    @{ Name = 'Ксения — ДОВЕРЕННАЯ ЗАДАЧА'; Target = 'TRUST-NEXT-TASK.cmd'; Hotkey = 'CTRL+ALT+D'; Description = 'После предупреждения разрешить следующую задачу без повторных подтверждений. Горячая клавиша Control Alt D.' },
    @{ Name = 'Ксения — локальная сеть'; Target = 'START-LAN.cmd'; Description = 'Открыть защищённую панель Ксении для телефона.' },
    @{ Name = 'Ксения — проверка микрофона'; Target = 'TEST-MICROPHONE.cmd'; Hotkey = 'CTRL+ALT+M'; Description = 'Проверить одну фразу с микрофона. Горячая клавиша Control Alt M.' },
    @{ Name = 'Ксения — проверка активации'; Target = 'TEST-WAKE-WORD.cmd'; Description = 'Отдельно проверить фразу Ксения слушай.' },
    @{ Name = 'Ксения — проверка голоса'; Target = 'TEST-VOICE.cmd'; Description = 'Прослушать русские голоса Silero.' },
    @{ Name = 'Ксения — проверка кнопки наушников'; Target = 'TEST-HEADSET-CONTROLS.cmd'; Description = 'Определить сенсорный жест JBL и включить им голосовую активацию.' },
    @{ Name = 'Ксения — список микрофонов'; Target = 'AUDIO-DEVICES.cmd'; Description = 'Показать все доступные устройства записи.' },
    @{ Name = 'Ксения — полный аудит'; Target = 'AUDIT.cmd'; Description = 'Проверить Python, голос, модели и программу.' },
    @{ Name = 'Ксения — вход в сайты'; Target = 'BROWSER-PROFILE.cmd'; Description = 'Открыть отдельный браузер Ксении для входа в нужные сайты.' }
)
foreach ($item in $shortcuts) {
    $link = $shell.CreateShortcut((Join-Path $desktop ($item.Name + '.lnk')))
    $link.TargetPath = Join-Path $projectRoot $item.Target
    $link.WorkingDirectory = $projectRoot
    $link.IconLocation = "$kseniaIcon,0"
    $link.Description = $item.Description
    $link.WindowStyle = 1
    $link.Hotkey = if ($item.Hotkey) { $item.Hotkey } else { '' }
    $link.Save()
    $saved = $shell.CreateShortcut((Join-Path $desktop ($item.Name + '.lnk')))
    if (
        [IO.Path]::GetFullPath($saved.TargetPath) -ne
        [IO.Path]::GetFullPath((Join-Path $projectRoot $item.Target)) -or
        [IO.Path]::GetFullPath($saved.WorkingDirectory) -ne
        [IO.Path]::GetFullPath($projectRoot)
    ) {
        throw "Ярлык не прошёл проверку после сохранения: $($item.Name)"
    }
}
Write-Host "Ярлыки Ксении созданы: $desktop"
