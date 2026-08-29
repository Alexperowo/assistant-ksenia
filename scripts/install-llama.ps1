param(
    [string]$ProjectRoot = '',
    [string]$CacheRoot = '',
    [string]$BackupRoot = '',
    [switch]$Force,
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$toolsRoot = Join-Path $ProjectRoot 'tools'
if (-not $CacheRoot) { $CacheRoot = Join-Path $toolsRoot 'downloads' }
$CacheRoot = [IO.Path]::GetFullPath($CacheRoot)
$installDir = Join-Path $toolsRoot 'llama.cpp'
$lockPath = Join-Path $ProjectRoot 'config\engine.lock.json'
$lock = Get-Content -Raw -Encoding UTF8 -LiteralPath $lockPath | ConvertFrom-Json
$release = [string]$lock.release
$commit = [string]$lock.commit
$cudaVersion = [string]$lock.cuda
$buildNumber = $release.TrimStart('b', 'B')
$binaryName = "llama-$release-bin-win-cuda-$cudaVersion-x64.zip"
$runtimeName = "cudart-llama-bin-win-cuda-$cudaVersion-x64.zip"
$binaryZip = Join-Path $CacheRoot $binaryName
$runtimeZip = Join-Path $CacheRoot $runtimeName
$baseUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$release"
$expectedHashes = @{}
foreach ($property in $lock.assets.PSObject.Properties) {
    $expectedHashes[$property.Name] = [string]$property.Value
}
if (-not $expectedHashes.ContainsKey($binaryName) -or -not $expectedHashes.ContainsKey($runtimeName)) {
    throw 'В engine.lock.json отсутствуют контрольные суммы архивов llama.cpp.'
}

function Assert-ChildPath {
    param([string]$Parent, [string]$Child)
    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    $childPath = [IO.Path]::GetFullPath($Child)
    if (-not $childPath.StartsWith($parentPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Небезопасный путь вне ожидаемого каталога: $childPath"
    }
}

function Get-EngineVersionText {
    param([string]$Directory)
    $server = Join-Path $Directory 'llama-server.exe'
    if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { return '' }
    $previousErrorAction = $ErrorActionPreference
    try {
        # llama-server writes its version to stderr. Windows PowerShell 5.1
        # wraps native stderr as ErrorRecord when the global mode is Stop.
        $ErrorActionPreference = 'Continue'
        $items = @(& $server --version 2>&1)
        $exitCode = $LASTEXITCODE
        $text = (($items | ForEach-Object { $_.ToString() }) -join "`n").Trim()
        if ($exitCode -ne 0) { return '' }
        return $text
    }
    catch { return '' }
    finally { $ErrorActionPreference = $previousErrorAction }
}

function Test-ExpectedVersion {
    param([string]$Text)
    $escapedBuild = [Regex]::Escape($buildNumber)
    $escapedCommit = [Regex]::Escape($commit)
    $legacyFormat = $Text -match "version:\s*$escapedBuild\s+\($escapedCommit\)"
    $currentFormat = $Text -match "version:\s*\S+\s+\(build\s+$escapedBuild,\s+commit\s+$escapedCommit\)"
    return $legacyFormat -or $currentFormat
}

function Test-Archive {
    param([string]$Path, [string]$ExpectedHash)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash -eq $ExpectedHash
}

function Move-ToQuarantine {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $quarantine = Join-Path $CacheRoot 'quarantine'
    New-Item -ItemType Directory -Force -Path $quarantine | Out-Null
    $destination = Join-Path $quarantine ((Split-Path -Leaf $Path) + '.invalid-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Assert-ChildPath $CacheRoot $destination
    Move-Item -LiteralPath $Path -Destination $destination
    Write-Warning "Повреждённый архив изолирован: $destination"
}

function Get-ReleaseArchive {
    param([string]$Name, [string]$Destination, [string]$ExpectedHash)
    if (Test-Archive $Destination $ExpectedHash) {
        Write-Host "Проверенный архив уже существует: $Destination"
        return
    }
    if (Test-Path -LiteralPath $Destination) { Move-ToQuarantine $Destination }
    if ($Offline) {
        throw "Offline-режим: отсутствует проверенный архив $Name в $CacheRoot"
    }
    New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
    $partial = "$Destination.partial"
    if (Test-Path -LiteralPath $partial) { Move-ToQuarantine $partial }
    Write-Host "Загрузка $Name..."
    try {
        Start-BitsTransfer -Source "$baseUrl/$Name" -Destination $partial
    }
    catch {
        Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/$Name" -OutFile $partial -TimeoutSec 900
    }
    if (-not (Test-Archive $partial $ExpectedHash)) {
        Move-ToQuarantine $partial
        throw "Контрольная сумма не совпала: $Name"
    }
    Move-Item -LiteralPath $partial -Destination $Destination
}

$currentVersion = Get-EngineVersionText $installDir
if ((Test-ExpectedVersion $currentVersion) -and -not $Force) {
    Write-Host "llama.cpp уже соответствует lock-файлу: $release ($commit)."
    exit 0
}

$expectedServer = [IO.Path]::GetFullPath((Join-Path $installDir 'llama-server.exe'))
foreach ($process in Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue) {
    if (-not $process.ExecutablePath) { continue }
    if ([IO.Path]::GetFullPath([string]$process.ExecutablePath) -eq $expectedServer) {
        throw "Нельзя обновить работающий llama.cpp. Сначала безопасно остановите модель PID $($process.ProcessId)."
    }
}

Get-ReleaseArchive -Name $binaryName -Destination $binaryZip -ExpectedHash $expectedHashes[$binaryName]
Get-ReleaseArchive -Name $runtimeName -Destination $runtimeZip -ExpectedHash $expectedHashes[$runtimeName]

$stageRoot = Join-Path $toolsRoot ('.llama-stage-' + [Guid]::NewGuid().ToString('N'))
$stageEngine = Join-Path $stageRoot 'llama.cpp'
Assert-ChildPath $toolsRoot $stageRoot
New-Item -ItemType Directory -Force -Path $stageEngine | Out-Null

if (-not $BackupRoot) {
    $BackupRoot = Join-Path $ProjectRoot ('runtime\updates\engine-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$BackupRoot = [IO.Path]::GetFullPath($BackupRoot)
$backupEngine = Join-Path $BackupRoot 'llama.cpp'
$oldMoved = $false

try {
    Write-Host "Стадийная распаковка llama.cpp $release..."
    Expand-Archive -LiteralPath $binaryZip -DestinationPath $stageEngine
    Expand-Archive -LiteralPath $runtimeZip -DestinationPath $stageEngine
    $stagedVersion = Get-EngineVersionText $stageEngine
    if (-not (Test-ExpectedVersion $stagedVersion)) {
        throw "Распакованный llama-server не соответствует $release ($commit): $stagedVersion"
    }
    $metadata = [ordered]@{
        schema_version = 1
        installed_at = [DateTimeOffset]::Now.ToString('o')
        release = $release
        commit = $commit
        cuda = $cudaVersion
        version_output = $stagedVersion
        archives = @(
            [ordered]@{ name = $binaryName; sha256 = $expectedHashes[$binaryName] },
            [ordered]@{ name = $runtimeName; sha256 = $expectedHashes[$runtimeName] }
        )
    }
    [IO.File]::WriteAllText(
        (Join-Path $stageEngine '.ksenia-engine.json'),
        (($metadata | ConvertTo-Json -Depth 8) + "`n"),
        $utf8
    )

    if (Test-Path -LiteralPath $installDir) {
        if (Test-Path -LiteralPath $backupEngine) {
            throw "Каталог резервной копии уже занят: $backupEngine"
        }
        New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
        Move-Item -LiteralPath $installDir -Destination $backupEngine
        $oldMoved = $true
    }
    Move-Item -LiteralPath $stageEngine -Destination $installDir
    $installedVersion = Get-EngineVersionText $installDir
    if (-not (Test-ExpectedVersion $installedVersion)) {
        throw 'Проверка установленного llama.cpp после переключения не прошла.'
    }
    Write-Host "Установлен llama.cpp $release ($commit)."
    if ($oldMoved) { Write-Host "Предыдущий движок сохранён: $backupEngine" }
}
catch {
    if ($oldMoved -and (Test-Path -LiteralPath $backupEngine)) {
        if (Test-Path -LiteralPath $installDir) {
            $failedEngine = Join-Path $BackupRoot ('failed-engine-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            Assert-ChildPath $BackupRoot $failedEngine
            Move-Item -LiteralPath $installDir -Destination $failedEngine
        }
        Move-Item -LiteralPath $backupEngine -Destination $installDir
        Write-Warning 'Предыдущий llama.cpp автоматически восстановлен.'
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Assert-ChildPath $toolsRoot $stageRoot
        if ((Split-Path -Leaf $stageRoot) -notlike '.llama-stage-*') {
            throw "Отказ от очистки неоднозначного staging: $stageRoot"
        }
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
