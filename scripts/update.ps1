param(
    [switch]$CheckOnly,
    [switch]$SkipAudit,
    [switch]$NoRestart,
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$updatesRoot = Join-Path $projectRoot 'runtime\updates'

function Find-Python {
    param([string]$Preferred)
    $candidates = [Collections.Generic.List[string]]::new()
    if ($Preferred) { $candidates.Add($Preferred) }
    $userConfigPath = Join-Path $projectRoot 'config\user.json'
    if (Test-Path -LiteralPath $userConfigPath) {
        try {
            $user = Get-Content -Raw -Encoding UTF8 -LiteralPath $userConfigPath | ConvertFrom-Json
            if ($user.voice.python) { $candidates.Add([string]$user.voice.python) }
        }
        catch {}
    }
    $candidates.Add('D:\AI\Butler\venv\Scripts\python.exe')
    $candidates.Add('C:\butler-venv\Scripts\python.exe')
    $candidates.Add((Join-Path $env:LocalAppData 'Ksenia\Butler\venv\Scripts\python.exe'))
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        & $candidate --version *> $null
        if ($LASTEXITCODE -eq 0) { return [IO.Path]::GetFullPath($candidate) }
    }
    throw 'Рабочая среда Python Ксении не найдена. Используйте INSTALL.cmd.'
}

function Get-Status {
    $raw = & $PythonPath (Join-Path $PSScriptRoot 'maintenance.py') status --root $projectRoot
    if ($LASTEXITCODE -ne 0) { throw "Не удалось проверить компоненты: $raw" }
    return ($raw | Out-String | ConvertFrom-Json)
}

function Show-Status {
    param([object]$Status)
    $engineText = if ($Status.engine.matches) { 'совпадает' } else { 'требует обновления' }
    $pythonText = if ($Status.python.matches) { 'совпадает' } else { 'требует обновления' }
    Write-Host "llama.cpp: $engineText; ожидается $($Status.engine.expected_release) ($($Status.engine.expected_commit))."
    Write-Host "Python: $pythonText; установлен $($Status.python.actual), ожидается $($Status.python.expected)."
    $mismatches = @($Status.packages | Where-Object { -not $_.matches })
    if ($mismatches.Count -eq 0) {
        Write-Host 'Python-библиотеки совпадают с lock-файлами.'
    } else {
        Write-Host 'Отличающиеся Python-библиотеки:'
        foreach ($item in $mismatches) {
            Write-Host "- $($item.name): установлено $($item.actual), ожидается $($item.expected)"
        }
    }
}

function Write-Metadata {
    param([System.Collections.IDictionary]$Value, [string]$Path)
    $temporary = "$Path.tmp"
    [IO.File]::WriteAllText($temporary, (($Value | ConvertTo-Json -Depth 12) + "`n"), $utf8)
    Move-Item -Force -LiteralPath $temporary -Destination $Path
}

$PythonPath = Find-Python $PythonPath
$status = Get-Status
Show-Status $status
if ($CheckOnly) {
    if ($status.all_components_match) {
        Write-Host 'Проверка завершена: обновление компонентов не требуется.'
    } else {
        Write-Host 'Проверка завершена: UPDATE.cmd может применить одобренные версии.'
    }
    exit 0
}

if ($status.all_components_match) {
    Write-Host 'Все компоненты уже соответствуют текущему релизу.'
    if (-not $SkipAudit) {
        & (Join-Path $PSScriptRoot 'check.ps1')
        exit $LASTEXITCODE
    }
    exit 0
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$updateDir = Join-Path $updatesRoot $stamp
New-Item -ItemType Directory -Force -Path $updateDir | Out-Null
$metadataPath = Join-Path $updateDir 'metadata.json'
$activeRole = if ($status.model) { [string]$status.model.role } else { $null }
$pipBefore = @($status.packages | Where-Object { $_.name -eq 'pip' } | Select-Object -First 1)
$metadata = [ordered]@{
    schema_version = 1
    update_id = $stamp
    project_root = $projectRoot
    started_at = [DateTimeOffset]::Now.ToString('o')
    status = 'started'
    active_role_before = $activeRole
    voice_was_running = [bool]$status.voice_state_present
    engine_changed = $false
    runtime_changed = $false
    pip_before = if ($pipBefore.Count) { [string]$pipBefore[0].actual } else { $null }
    engine_backup = $null
    config_backup = $null
    freeze_backup = $null
    error = $null
}
Write-Metadata $metadata $metadataPath

$userConfigPath = Join-Path $projectRoot 'config\user.json'
if (Test-Path -LiteralPath $userConfigPath -PathType Leaf) {
    $configBackup = Join-Path $updateDir 'user.json.before'
    Copy-Item -LiteralPath $userConfigPath -Destination $configBackup
    $metadata.config_backup = $configBackup
}
$freezeBackup = Join-Path $updateDir 'python-freeze-before.txt'
& $PythonPath -m pip freeze | Set-Content -LiteralPath $freezeBackup -Encoding UTF8
if ($LASTEXITCODE -ne 0) { throw 'Не удалось сохранить список Python-пакетов перед обновлением.' }
$metadata.freeze_backup = $freezeBackup
Write-Metadata $metadata $metadataPath

try {
    if ($status.voice_state_present) {
        & (Join-Path $PSScriptRoot 'stop-voice.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось безопасно остановить голосовой режим.' }
    }
    if ($status.model) {
        $stopResult = & $PythonPath (Join-Path $PSScriptRoot 'maintenance.py') stop-model --root $projectRoot
        if ($LASTEXITCODE -ne 0) { throw "Не удалось безопасно остановить модель: $stopResult" }
    }

    if (-not $status.engine.matches) {
        $managedServer = [IO.Path]::GetFullPath((Join-Path $projectRoot 'tools\llama.cpp\llama-server.exe'))
        if ([IO.Path]::GetFullPath([string]$status.engine.path) -ne $managedServer) {
            throw "Настроен внешний llama-server; автоматическое обновление запрещено: $($status.engine.path)"
        }
        $engineBackupRoot = Join-Path $updateDir 'engine-backup'
        & (Join-Path $PSScriptRoot 'install-llama.ps1') -Force -BackupRoot $engineBackupRoot
        if ($LASTEXITCODE -ne 0) { throw 'Стадийное обновление llama.cpp завершилось ошибкой.' }
        $metadata.engine_changed = $true
        $metadata.engine_backup = Join-Path $engineBackupRoot 'llama.cpp'
        Write-Metadata $metadata $metadataPath
    }

    if (-not $status.python.matches -or -not $status.packages_match) {
        $playwrightMismatch = @($status.packages | Where-Object { $_.name -eq 'playwright' -and -not $_.matches }).Count -gt 0
        $metadata.runtime_changed = $true
        Write-Metadata $metadata $metadataPath
        $arguments = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $PSScriptRoot 'install-runtime.ps1'),
            '-SkipAudit', '-SkipShortcuts', '-SkipLlama'
        )
        if ($playwrightMismatch) { $arguments += '-RefreshBrowser' }
        & powershell.exe @arguments
        if ($LASTEXITCODE -ne 0) { throw 'Обновление Python/browser/voice runtime завершилось ошибкой.' }
        $PythonPath = Find-Python ''
    }

    if ($activeRole -and -not $NoRestart) {
        $startResult = & $PythonPath (Join-Path $PSScriptRoot 'maintenance.py') start-model --root $projectRoot --role $activeRole
        if ($LASTEXITCODE -ne 0) { throw "Не удалось вернуть модель роли ${activeRole}: $startResult" }
    }

    $after = Get-Status
    if (-not $after.all_components_match) {
        throw 'После обновления компоненты всё ещё расходятся с lock-файлами.'
    }
    if (-not $SkipAudit) {
        & (Join-Path $PSScriptRoot 'check.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Полный аудит после обновления не прошёл.' }
    }

    $metadata.status = 'succeeded'
    $metadata.completed_at = [DateTimeOffset]::Now.ToString('o')
    Write-Metadata $metadata $metadataPath
    $latest = [ordered]@{
        schema_version = 1
        update_directory = $updateDir
        status = 'succeeded'
        completed_at = $metadata.completed_at
    }
    Write-Metadata $latest (Join-Path $updatesRoot 'latest.json')
    Write-Host "Обновление успешно. Запись: $metadataPath"
    if ($status.voice_state_present) {
        Write-Host 'Голосовой режим оставлен выключенным; запустите его обычным ярлыком после проверки.'
    }
    exit 0
}
catch {
    $metadata.status = 'failed'
    $metadata.failed_at = [DateTimeOffset]::Now.ToString('o')
    $metadata.error = $_.Exception.Message
    Write-Metadata $metadata $metadataPath
    Write-Warning "Обновление не прошло: $($_.Exception.Message)"
    try {
        & (Join-Path $PSScriptRoot 'rollback-update.ps1') -UpdateDirectory $updateDir -Automatic -SkipAudit
    }
    catch {
        Write-Warning "Автоматический откат также требует внимания: $($_.Exception.Message)"
    }
    throw "Обновление отменено. Запись: $metadataPath"
}
