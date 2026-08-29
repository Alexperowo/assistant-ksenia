param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$BackendName,
    [Parameter(Mandatory = $true)]
    [string]$SourceDirectory,
    [string]$ProjectRoot = '',
    [string]$BackupRoot = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$SourceDirectory = [IO.Path]::GetFullPath($SourceDirectory)
$toolsRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot 'tools'))
$lockPath = Join-Path $ProjectRoot 'config\engine.lock.json'
$lock = Get-Content -Raw -Encoding UTF8 -LiteralPath $lockPath | ConvertFrom-Json
$backendProperty = $lock.backends.PSObject.Properties[$BackendName]
if ($null -eq $backendProperty) {
    throw "В engine.lock.json отсутствует backend '$BackendName'."
}
$backend = $backendProperty.Value
if ([string]$backend.distribution -ne 'verified-local-build') {
    throw "Backend '$BackendName' не устанавливается из локальной сборки."
}
$relativeExecutable = [string]$backend.executable
if ([IO.Path]::IsPathRooted($relativeExecutable)) {
    throw "Путь backend-а '$BackendName' в lock-файле должен быть относительным."
}
$expectedExecutable = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $relativeExecutable))
$installDir = Split-Path -Parent $expectedExecutable

function Assert-ChildPath {
    param([string]$Parent, [string]$Child)
    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    $childPath = [IO.Path]::GetFullPath($Child)
    if (-not $childPath.StartsWith($parentPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Небезопасный путь вне ожидаемого каталога: $childPath"
    }
}

Assert-ChildPath $toolsRoot $installDir
if (-not (Test-Path -LiteralPath $SourceDirectory -PathType Container)) {
    throw "Не найден каталог проверяемой сборки: $SourceDirectory"
}

$runtimeFiles = @($backend.runtime_files.PSObject.Properties)
if ($runtimeFiles.Count -eq 0) {
    throw "Backend '$BackendName' не содержит runtime_files."
}

function Test-RuntimeDirectory {
    param([string]$Directory, [switch]$ThrowOnMismatch)
    foreach ($property in $runtimeFiles) {
        $name = [string]$property.Name
        if ([IO.Path]::GetFileName($name) -ne $name) {
            throw "Небезопасное имя runtime-файла: $name"
        }
        $path = Join-Path $Directory $name
        $expectedSize = [int64]$property.Value.size
        $expectedHash = ([string]$property.Value.sha256).ToUpperInvariant()
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            if ($ThrowOnMismatch) { throw "Отсутствует runtime-файл: $path" }
            return $false
        }
        $actualSize = (Get-Item -LiteralPath $path).Length
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash
        if ($actualSize -ne $expectedSize -or $actualHash -ne $expectedHash) {
            if ($ThrowOnMismatch) { throw "Размер или SHA-256 не совпал: $path" }
            return $false
        }
    }
    return $true
}

function Test-ExpectedVersion {
    param([string]$Directory, [switch]$ThrowOnMismatch)
    $server = Join-Path $Directory 'llama-server.exe'
    if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { return $false }
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $server
    $startInfo.Arguments = '--version'
    $startInfo.WorkingDirectory = $Directory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($startInfo)
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $text = ($stdout + $stderr).Trim()
    if ($process.ExitCode -ne 0) {
        if ($ThrowOnMismatch) {
            throw "Backend '$BackendName' --version failed with exit code $($process.ExitCode)."
        }
        return $false
    }
    $build = [regex]::Escape([string]$backend.version_build)
    $commit = [regex]::Escape([string]$backend.version_commit)
    $matches = $text -match "version:\s*$build\s+\($commit\)" -or
        $text -match "version:\s*\S+\s+\(build\s+$build,\s+commit\s+$commit\)"
    if (-not $matches -and $ThrowOnMismatch) {
        throw "Версия backend-а '$BackendName' не совпала с lock-файлом: $text"
    }
    return $matches
}

$patchRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot 'config\patches'))
$patchPath = [IO.Path]::GetFullPath((Join-Path $ProjectRoot ([string]$backend.patch.path)))
Assert-ChildPath $patchRoot $patchPath
if (-not (Test-Path -LiteralPath $patchPath -PathType Leaf)) {
    throw "Не найден закреплённый patch backend-а: $patchPath"
}
$patchHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $patchPath).Hash
if ($patchHash -ne ([string]$backend.patch.sha256).ToUpperInvariant()) {
    throw "SHA-256 patch backend-а '$BackendName' не совпал с lock-файлом."
}

Test-RuntimeDirectory $SourceDirectory -ThrowOnMismatch | Out-Null
if (-not (Test-ExpectedVersion $SourceDirectory -ThrowOnMismatch)) {
    throw "Проверяемая сборка backend-а '$BackendName' имеет неверную версию."
}
if ((Test-RuntimeDirectory $installDir) -and (Test-ExpectedVersion $installDir) -and -not $Force) {
    Write-Host "Backend '$BackendName' уже соответствует lock-файлу."
    exit 0
}

foreach ($process in Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue) {
    if (-not $process.ExecutablePath) { continue }
    if ([IO.Path]::GetFullPath([string]$process.ExecutablePath) -eq $expectedExecutable) {
        throw "Нельзя заменить работающий backend '$BackendName'. Сначала безопасно остановите модель PID $($process.ProcessId)."
    }
}

$stageRoot = Join-Path $toolsRoot ('.backend-stage-' + $BackendName + '-' + [Guid]::NewGuid().ToString('N'))
$stageEngine = Join-Path $stageRoot 'engine'
Assert-ChildPath $toolsRoot $stageRoot
New-Item -ItemType Directory -Force -Path $stageEngine | Out-Null
if (-not $BackupRoot) {
    $BackupRoot = Join-Path $ProjectRoot ('runtime\updates\engine-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '\' + $BackendName)
}
$BackupRoot = [IO.Path]::GetFullPath($BackupRoot)
$backupEngine = Join-Path $BackupRoot (Split-Path -Leaf $installDir)
$oldMoved = $false

try {
    foreach ($property in $runtimeFiles) {
        Copy-Item -LiteralPath (Join-Path $SourceDirectory $property.Name) -Destination $stageEngine
    }
    Test-RuntimeDirectory $stageEngine -ThrowOnMismatch | Out-Null
    Test-ExpectedVersion $stageEngine -ThrowOnMismatch | Out-Null
    $metadata = [ordered]@{
        schema_version = 1
        backend = $BackendName
        installed_at = [DateTimeOffset]::Now.ToString('o')
        repository = [string]$backend.repository
        branch = [string]$backend.branch
        commit = [string]$backend.commit
        patch_path = [string]$backend.patch.path
        patch_sha256 = [string]$backend.patch.sha256
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
    Test-RuntimeDirectory $installDir -ThrowOnMismatch | Out-Null
    Test-ExpectedVersion $installDir -ThrowOnMismatch | Out-Null
    Write-Host "Установлен backend '$BackendName' из проверенной локальной сборки."
    if ($oldMoved) { Write-Host "Предыдущий backend сохранён: $backupEngine" }
}
catch {
    if ($oldMoved -and (Test-Path -LiteralPath $backupEngine)) {
        if (Test-Path -LiteralPath $installDir) {
            $failedEngine = Join-Path $BackupRoot ('failed-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            Move-Item -LiteralPath $installDir -Destination $failedEngine
        }
        Move-Item -LiteralPath $backupEngine -Destination $installDir
        Write-Warning "Предыдущий backend '$BackendName' автоматически восстановлен."
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Assert-ChildPath $toolsRoot $stageRoot
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
