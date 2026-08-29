param(
    [string]$CacheRoot = '',
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$PythonPath = Resolve-KseniaPython -ProjectRoot $projectRoot -ExplicitPath $PythonPath
if (-not $PythonPath) { throw 'Python 3.12 для теста обновления не найден.' }
if (-not $CacheRoot) { $CacheRoot = Join-Path $projectRoot 'tools\downloads' }
$CacheRoot = [IO.Path]::GetFullPath($CacheRoot)
$testParent = Join-Path $projectRoot 'runtime\maintenance-tests'
$sandbox = Join-Path $testParent ([Guid]::NewGuid().ToString('N'))
$sandbox = [IO.Path]::GetFullPath($sandbox)

function Assert-SandboxPath {
    param([string]$Path)
    $parent = [IO.Path]::GetFullPath($testParent).TrimEnd('\')
    $child = [IO.Path]::GetFullPath($Path)
    if (-not $child.StartsWith($parent + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Небезопасный тестовый путь: $child"
    }
}

Assert-SandboxPath $sandbox
try {
    New-Item -ItemType Directory -Force -Path (Join-Path $sandbox 'config') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $sandbox 'requirements') | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config\engine.lock.json') -Destination (Join-Path $sandbox 'config\engine.lock.json')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config\default.json') -Destination (Join-Path $sandbox 'config\default.json')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config\runtime-assets.lock.json') -Destination (Join-Path $sandbox 'config\runtime-assets.lock.json')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'requirements\runtime.lock.txt') -Destination (Join-Path $sandbox 'requirements\runtime.lock.txt')
    $runtimeAssets = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $projectRoot 'config\runtime-assets.lock.json') | ConvertFrom-Json
    $torchRequirements = [string]$runtimeAssets.torch.requirements
    Copy-Item -LiteralPath (Join-Path $projectRoot $torchRequirements) -Destination (Join-Path $sandbox $torchRequirements)

    & (Join-Path $PSScriptRoot 'install-llama.ps1') -ProjectRoot $sandbox -CacheRoot $CacheRoot -Offline
    if ($LASTEXITCODE -ne 0) { throw 'Первая стадийная установка не прошла.' }
    $server = Join-Path $sandbox 'tools\llama.cpp\llama-server.exe'
    $metadata = Join-Path $sandbox 'tools\llama.cpp\.ksenia-engine.json'
    if (-not (Test-Path -LiteralPath $server -PathType Leaf) -or -not (Test-Path -LiteralPath $metadata -PathType Leaf)) {
        throw 'Стадийная установка не создала server/metadata.'
    }
    $firstHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $server).Hash

    $updateDir = Join-Path $sandbox 'runtime\updates\test-update'
    $backupRoot = Join-Path $updateDir 'engine-backup'
    & (Join-Path $PSScriptRoot 'install-llama.ps1') -ProjectRoot $sandbox -CacheRoot $CacheRoot -BackupRoot $backupRoot -Offline -Force
    if ($LASTEXITCODE -ne 0) { throw 'Принудительное стадийное переключение не прошло.' }
    $backupServer = Join-Path $backupRoot 'llama.cpp\llama-server.exe'
    if (-not (Test-Path -LiteralPath $backupServer -PathType Leaf)) {
        throw 'Предыдущий движок не оказался в резервной копии.'
    }
    $currentHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $server).Hash
    $backupHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $backupServer).Hash
    if ($firstHash -ne $currentHash -or $firstHash -ne $backupHash) {
        throw 'Хеши движка после стадийного переключения расходятся.'
    }
    $updateMetadata = [ordered]@{
        schema_version = 2
        update_id = 'test-update'
        project_root = $sandbox
        status = 'succeeded'
        active_role_before = $null
        voice_was_running = $false
        engine_changed = $true
        runtime_changed = $false
        engine_existed_before = $true
        engine_sha256_before = $firstHash.ToLowerInvariant()
        engine_version_before = $null
        python_before = $null
        pip_before = $null
        engine_backup = Join-Path $backupRoot 'llama.cpp'
        config_backup = $null
        freeze_backup = $null
        error = $null
    }
    $metadataPath = Join-Path $updateDir 'metadata.json'
    [IO.File]::WriteAllText($metadataPath, (($updateMetadata | ConvertTo-Json -Depth 8) + "`n"), $utf8)
    & (Join-Path $PSScriptRoot 'rollback-update.ps1') -ProjectRoot $sandbox -UpdateDirectory $updateDir -PythonPath $PythonPath -SkipAudit -NoRestart
    if ($LASTEXITCODE -ne 0) { throw 'Проверка реального отката не прошла.' }
    $rolledHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $server).Hash
    $rolledMetadata = Get-Content -Raw -Encoding UTF8 -LiteralPath $metadataPath | ConvertFrom-Json
    $displaced = @(Get-ChildItem -LiteralPath $updateDir -Directory -Filter 'rollback-displaced-engine-*')
    if ($rolledHash -ne $firstHash -or $rolledMetadata.status -ne 'rolled_back' -or $displaced.Count -ne 1) {
        throw 'Откат не подтвердил движок, metadata и сохранение вытеснённой версии.'
    }
    [ordered]@{
        ok = $true
        staged_install = $true
        forced_swap = $true
        backup_verified = $true
        rollback_verified = $true
        engine_sha256 = $currentHash
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $sandbox) {
        Assert-SandboxPath $sandbox
        Remove-Item -LiteralPath $sandbox -Recurse -Force
    }
}
