param(
    [string]$ArchivePath = '',
    [switch]$KeepWorkingDirectory
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ArchivePath) {
    $ArchivePath = Join-Path $projectRoot 'dist\Ksenia-0.1.0-online.zip'
}
$ArchivePath = [IO.Path]::GetFullPath($ArchivePath)
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Release archive not found: $ArchivePath"
}

$testParent = [IO.Path]::GetFullPath((Join-Path $projectRoot 'runtime\distribution-tests'))
New-Item -ItemType Directory -Force -Path $testParent | Out-Null
$testRoot = Join-Path $testParent ('clean-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null

function Remove-SafeTestDirectory {
    param([string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = $testParent.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe cleanup target: $resolved"
    }
    if ((Split-Path -Leaf $resolved) -notlike 'clean-*') {
        throw "Unexpected cleanup directory: $resolved"
    }
    Start-Sleep -Milliseconds 1000
    $lastFailure = $null
    for ($attempt = 1; $attempt -le 4 -and (Test-Path -LiteralPath $resolved); $attempt++) {
        try {
            [IO.Directory]::Delete($resolved, $true)
        }
        catch {
            $lastFailure = $_
            Start-Sleep -Milliseconds (500 * $attempt)
        }
    }
    if (Test-Path -LiteralPath $resolved) {
        try {
            [IO.Directory]::Delete(('\\?\' + $resolved), $true)
        }
        catch {
            $lastFailure = $_
        }
    }
    if (Test-Path -LiteralPath $resolved) {
        throw "Cleanup failed for $resolved. $($lastFailure.Exception.Message)"
    }
}

try {
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $testRoot
    $packageDirectories = @(Get-ChildItem -LiteralPath $testRoot -Directory)
    if ($packageDirectories.Count -ne 1) {
        throw 'Archive must contain exactly one top-level directory.'
    }
    $packageRoot = $packageDirectories[0].FullName
    $installRoot = Join-Path $testRoot 'installed-runtime'
    $modelRoot = Join-Path $testRoot 'external-models'
    $installer = Join-Path $packageRoot 'scripts\install-runtime.ps1'

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer `
        -ProjectRoot $packageRoot `
        -InstallRoot $installRoot `
        -ModelStorageRoot $modelRoot `
        -SkipAudit -SkipShortcuts -SkipLlama -SkipSpeechModels
    if ($LASTEXITCODE -ne 0) {
        throw "Installer smoke test failed with code $LASTEXITCODE."
    }

    $userConfigPath = Join-Path $packageRoot 'config\user.json'
    if (-not (Test-Path -LiteralPath $userConfigPath -PathType Leaf)) {
        throw 'Installer did not create config/user.json.'
    }
    $userConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $userConfigPath | ConvertFrom-Json
    if ([IO.Path]::GetFullPath([string]$userConfig.paths.models_dir) -ne [IO.Path]::GetFullPath($modelRoot)) {
        throw 'ModelStorageRoot was not preserved.'
    }
    if (-not (Test-Path -LiteralPath ([string]$userConfig.browser.executable) -PathType Leaf)) {
        throw 'Chromium was not installed or recorded.'
    }
    if (-not (Test-Path -LiteralPath ([string]$userConfig.browser.profile_dir) -PathType Container)) {
        throw 'Browser profile was not created.'
    }
    if (Get-ChildItem -LiteralPath $packageRoot -Recurse -File | Where-Object { $_.Extension -in @('.gguf', '.safetensors') }) {
        throw 'A heavy model appeared inside the project package.'
    }

    [pscustomobject]@{
        ok = $true
        archive = $ArchivePath
        clean_user_config_created = $true
        isolated_model_root = $true
        chromium_ready = $true
        heavy_models_skipped = $true
        working_directory = $testRoot
    } | ConvertTo-Json -Compress
}
finally {
    if ($KeepWorkingDirectory) {
        Write-Host "Working directory preserved: $testRoot"
    } else {
        Remove-SafeTestDirectory $testRoot
    }
}
