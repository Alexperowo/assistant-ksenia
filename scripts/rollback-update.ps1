param(
    [string]$UpdateDirectory = '',
    [switch]$Automatic,
    [switch]$SkipAudit,
    [switch]$NoRestart,
    [string]$PythonPath = '',
    [string]$ProjectRoot = ''
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$projectRoot = if ($ProjectRoot) {
    [IO.Path]::GetFullPath($ProjectRoot)
} else {
    [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
}
$updatesRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'runtime\updates'))
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')

function Assert-ChildPath {
    param([string]$Parent, [string]$Child)
    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    $childPath = [IO.Path]::GetFullPath($Child)
    if (-not $childPath.StartsWith($parentPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Небезопасный путь отката: $childPath"
    }
}

function Write-Metadata {
    param([object]$Value, [string]$Path)
    $parent = Split-Path -Parent $Path
    $temporary = Join-Path $parent ('.' + (Split-Path -Leaf $Path) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, (($Value | ConvertTo-Json -Depth 12) + "`n"), $utf8)
        Move-Item -Force -LiteralPath $temporary -Destination $Path
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $temporary
    }
}

if (-not $UpdateDirectory) {
    $latestPath = Join-Path $updatesRoot 'latest.json'
    if (Test-Path -LiteralPath $latestPath -PathType Leaf) {
        $latest = Get-Content -Raw -Encoding UTF8 -LiteralPath $latestPath | ConvertFrom-Json
        $UpdateDirectory = [string]$latest.update_directory
    } else {
        $candidate = Get-ChildItem -LiteralPath $updatesRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'metadata.json') } |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($candidate) { $UpdateDirectory = $candidate.FullName }
    }
}
if (-not $UpdateDirectory) { throw 'Не найдена сохранённая попытка обновления.' }
$UpdateDirectory = [IO.Path]::GetFullPath($UpdateDirectory)
Assert-ChildPath $updatesRoot $UpdateDirectory
$metadataPath = Join-Path $UpdateDirectory 'metadata.json'
if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
    throw "Не найден metadata.json: $metadataPath"
}
$metadata = Get-Content -Raw -Encoding UTF8 -LiteralPath $metadataPath | ConvertFrom-Json
$PythonPath = Resolve-KseniaPython -ProjectRoot $projectRoot -ExplicitPath $PythonPath
if (-not $PythonPath) { throw 'Python Ксении не найден для отката.' }

$currentStatusRaw = & $PythonPath (Join-Path $PSScriptRoot 'maintenance.py') status --root $projectRoot
if ($LASTEXITCODE -ne 0) { throw "Не удалось снять состояние перед откатом: $currentStatusRaw" }
$currentStatus = $currentStatusRaw | Out-String | ConvertFrom-Json
if ($currentStatus.voice_state_present) {
    & (Join-Path $PSScriptRoot 'stop-voice.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось остановить голос перед откатом.' }
}
if ($currentStatus.model) {
    & $PythonPath (Join-Path $PSScriptRoot 'maintenance.py') stop-model --root $projectRoot | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось остановить модель перед откатом.' }
}

$restoredSomething = $false
$backupEngine = [string]$metadata.engine_backup
$installEngine = Join-Path $projectRoot 'tools\llama.cpp'
$engineExistedBefore = if ($null -ne $metadata.engine_existed_before) {
    [bool]$metadata.engine_existed_before
} else {
    [bool]($backupEngine -and (Test-Path -LiteralPath $backupEngine -PathType Container))
}
if ($backupEngine -and (Test-Path -LiteralPath $backupEngine -PathType Container)) {
    $backupEngine = [IO.Path]::GetFullPath($backupEngine)
    Assert-ChildPath $UpdateDirectory $backupEngine
    if (Test-Path -LiteralPath $installEngine) {
        $displaced = Join-Path $UpdateDirectory ('rollback-displaced-engine-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Assert-ChildPath $UpdateDirectory $displaced
        Move-Item -LiteralPath $installEngine -Destination $displaced
    }
    Move-Item -LiteralPath $backupEngine -Destination $installEngine
    $restoredSomething = $true
    Write-Host 'Предыдущий llama.cpp восстановлен.'
} elseif ([bool]$metadata.engine_changed -and -not $engineExistedBefore) {
    if (Test-Path -LiteralPath $installEngine) {
        $displaced = Join-Path $UpdateDirectory ('rollback-displaced-engine-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Assert-ChildPath $UpdateDirectory $displaced
        Move-Item -LiteralPath $installEngine -Destination $displaced
    }
    $restoredSomething = $true
    Write-Host 'Возвращено исходное отсутствие управляемого llama.cpp.'
} elseif ([bool]$metadata.engine_changed) {
    throw 'Нельзя подтвердить откат llama.cpp: резервная копия предыдущего движка отсутствует.'
}

$configBackup = [string]$metadata.config_backup
if ($configBackup -and (Test-Path -LiteralPath $configBackup -PathType Leaf)) {
    $configBackup = [IO.Path]::GetFullPath($configBackup)
    Assert-ChildPath $UpdateDirectory $configBackup
    Copy-Item -Force -LiteralPath $configBackup -Destination (Join-Path $projectRoot 'config\user.json')
    $restoredSomething = $true
    Write-Host 'Личная конфигурация восстановлена из копии обновления.'
}

$freezeBackup = [string]$metadata.freeze_backup
if ([bool]$metadata.runtime_changed -and $freezeBackup -and (Test-Path -LiteralPath $freezeBackup -PathType Leaf)) {
    $freezeBackup = [IO.Path]::GetFullPath($freezeBackup)
    Assert-ChildPath $UpdateDirectory $freezeBackup
    if ($metadata.pip_before) {
        & $PythonPath -m pip install "pip==$([string]$metadata.pip_before)"
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось вернуть предыдущий pip.' }
    }
    $lines = @(Get-Content -LiteralPath $freezeBackup -Encoding UTF8 | Where-Object { $_.Trim() -and -not $_.StartsWith('#') })
    $torchLine = $lines | Where-Object { $_ -match '^(?i)torch==' } | Select-Object -First 1
    if ($torchLine) {
        & $PythonPath -m pip install --upgrade --index-url https://download.pytorch.org/whl/cu128 $torchLine
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось вернуть предыдущий Torch.' }
    }
    $restoreRequirements = Join-Path $UpdateDirectory 'python-freeze-restore.txt'
    $lines | Where-Object { $_ -notmatch '^(?i)torch==' } | Set-Content -LiteralPath $restoreRequirements -Encoding UTF8
    & $PythonPath -m pip install --upgrade --requirement $restoreRequirements
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось вернуть предыдущие Python-пакеты.' }
    & $PythonPath -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'После возврата Python-пакетов pip check не прошёл.' }
    $restoredSomething = $true
    Write-Host 'Предыдущие Python-пакеты восстановлены.'
}

if (-not $restoredSomething) {
    throw 'В выбранной записи нет компонентов, которые можно вернуть.'
}

$role = [string]$metadata.active_role_before
if ($role -and -not $NoRestart) {
    & $PythonPath (Join-Path $PSScriptRoot 'maintenance.py') start-model --root $projectRoot --role $role | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Компоненты возвращены, но модель роли $role не запустилась." }
}

$afterStatusRaw = & $PythonPath (Join-Path $PSScriptRoot 'maintenance.py') status --root $projectRoot
if ($LASTEXITCODE -ne 0) { throw "Не удалось проверить компоненты после отката: $afterStatusRaw" }
$afterStatus = $afterStatusRaw | Out-String | ConvertFrom-Json
if ([bool]$metadata.engine_changed -and $engineExistedBefore) {
    $restoredServer = [string]$afterStatus.engine.path
    if (-not (Test-Path -LiteralPath $restoredServer -PathType Leaf)) {
        throw 'Откат не вернул прежний исполняемый файл llama.cpp.'
    }
    $restoredHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $restoredServer).Hash.ToLowerInvariant()
    if (-not $metadata.engine_sha256_before -or $restoredHash -ne [string]$metadata.engine_sha256_before) {
        throw 'SHA-256 восстановленного llama.cpp не совпал со снимком до обновления.'
    }
    if ($metadata.engine_version_before -and [string]$afterStatus.engine.version_output -ne [string]$metadata.engine_version_before) {
        throw 'Версия восстановленного llama.cpp не совпала со снимком до обновления.'
    }
} elseif ([bool]$metadata.engine_changed -and (Test-Path -LiteralPath $installEngine)) {
    throw 'До обновления управляемого llama.cpp не было, но после отката папка осталась.'
}
if ([bool]$metadata.runtime_changed) {
    if ($metadata.python_before -and [string]$afterStatus.python.actual -ne [string]$metadata.python_before) {
        throw 'Версия Python после отката не совпала со снимком до обновления.'
    }
    $pipAfter = @($afterStatus.packages | Where-Object { $_.name -eq 'pip' } | Select-Object -First 1)
    if ($metadata.pip_before -and (-not $pipAfter.Count -or [string]$pipAfter[0].actual -ne [string]$metadata.pip_before)) {
        throw 'Версия pip после отката не совпала со снимком до обновления.'
    }
    $expectedFreeze = @(
        Get-Content -LiteralPath $freezeBackup -Encoding UTF8 |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $_.StartsWith('#') } |
            Sort-Object -Unique
    )
    $actualFreeze = @(& $PythonPath -m pip freeze)
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось проверить Python-пакеты после отката.' }
    $actualFreeze = @($actualFreeze | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Sort-Object -Unique)
    $freezeDifference = @(Compare-Object -ReferenceObject $expectedFreeze -DifferenceObject $actualFreeze)
    if ($freezeDifference.Count -ne 0) {
        throw 'Python-пакеты после отката не совпали с точным freeze-снимком.'
    }
}

$metadata | Add-Member -Force -NotePropertyName rollback_at -NotePropertyValue ([DateTimeOffset]::Now.ToString('o'))
$metadata | Add-Member -Force -NotePropertyName status -NotePropertyValue 'rolled_back'
Write-Metadata $metadata $metadataPath

if (-not $SkipAudit) {
    & (Join-Path $PSScriptRoot 'check.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Откат выполнен, но полный аудит требует внимания.' }
}
Write-Host "Откат завершён. Запись: $metadataPath"
if ($Automatic) { Write-Host 'Это был автоматический откат после неудачного обновления.' }
