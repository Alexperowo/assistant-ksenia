$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$statePath = Join-Path $projectRoot 'runtime\voice\agent.json'
$speak = Join-Path $PSScriptRoot 'speak.ps1'

function Say {
    param([string]$Text)
    try { & $speak -Text $Text } catch {}
}

if (-not (Test-Path -LiteralPath $statePath)) {
    Say 'Голосовой режим Ксении сейчас не запущен.'
    exit 0
}

try {
    $state = Get-Content -Raw -LiteralPath $statePath -Encoding UTF8 | ConvertFrom-Json
    $voicePid = [int]$state.pid
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$voicePid"
    if (-not $process) {
        Remove-Item -Force -LiteralPath $statePath
        Say 'Голосовой режим уже завершён.'
        exit 0
    }
    $expectedExecutable = [IO.Path]::GetFullPath([string]$state.executable)
    $actualExecutable = [IO.Path]::GetFullPath([string]$process.ExecutablePath)
    $commandLine = [string]$process.CommandLine
    $allowedExecutables = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    [void]$allowedExecutables.Add($expectedExecutable)

    # Старые файлы состояния хранили путь запуска из виртуальной среды.
    # В Windows он передаёт работу базовому python.exe из pyvenv.cfg.
    $venvRoot = Split-Path -Parent (Split-Path -Parent $expectedExecutable)
    $venvConfig = Join-Path $venvRoot 'pyvenv.cfg'
    if (Test-Path -LiteralPath $venvConfig) {
        $homeLine = Get-Content -LiteralPath $venvConfig -Encoding UTF8 |
            Where-Object { $_ -match '^\s*home\s*=\s*(.+?)\s*$' } |
            Select-Object -First 1
        if ($homeLine -and $homeLine -match '^\s*home\s*=\s*(.+?)\s*$') {
            $basePython = Join-Path $Matches[1].Trim() 'python.exe'
            if (Test-Path -LiteralPath $basePython) {
                [void]$allowedExecutables.Add([IO.Path]::GetFullPath($basePython))
            }
        }
    }
    if (
        -not $allowedExecutables.Contains($actualExecutable) -or
        $commandLine -notmatch '(?i)-m\s+butler\s+voice-agent'
    ) {
        Say 'Не удалось безопасно определить процесс Ксении. Запустите полный аудит.'
        exit 2
    }

    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$voicePid"
    foreach ($child in $children) {
        Stop-Process -Id ([int]$child.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $voicePid -Force
    Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $statePath
    Say 'Голосовой режим Ксении остановлен.'
    exit 0
}
catch {
    Say 'Не удалось остановить голосовой режим. Запустите полный аудит.'
    exit 3
}
