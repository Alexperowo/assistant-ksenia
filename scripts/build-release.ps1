param(
    [string]$DestinationRoot = '',
    [string]$PythonPath = '',
    [switch]$AllowDirtySource
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
if ((Test-Path -LiteralPath (Join-Path $projectRoot '.git')) -and -not $AllowDirtySource) {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) { $git = Get-Command git -ErrorAction SilentlyContinue }
    if (-not $git) {
        throw 'Git-репозиторий найден, но git недоступен для проверки чистоты исходников.'
    }
    $sourceStatus = @(& $git.Source -C $projectRoot status --porcelain=v1 --untracked-files=all)
    if ($LASTEXITCODE -ne 0) {
        throw 'Не удалось проверить состояние Git перед сборкой выпуска.'
    }
    if ($sourceStatus.Count -gt 0) {
        throw (
            'Сборка выпуска из изменённого дерева остановлена. Сначала проверьте и ' +
            'зафиксируйте изменения; -AllowDirtySource предназначен только для явной ' +
            'локальной диагностики.'
        )
    }
}
$manifestPath = Join-Path $projectRoot 'config\release-manifest.json'
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
$version = [string]$manifest.project.version
$packageName = "$([string]$manifest.project.package_name)-$version"
if (-not $DestinationRoot) { $DestinationRoot = Join-Path $projectRoot 'dist' }
$DestinationRoot = [IO.Path]::GetFullPath($DestinationRoot)
New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null

function Assert-ChildPath {
    param([string]$Parent, [string]$Child)
    $resolvedParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    $resolvedChild = [IO.Path]::GetFullPath($Child)
    if (-not $resolvedChild.StartsWith($resolvedParent + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Небезопасный путь вне каталога выпуска: $resolvedChild"
    }
}

function Get-RelativePathCompat {
    param([string]$BasePath, [string]$TargetPath)
    $base = [IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'
    $target = [IO.Path]::GetFullPath($TargetPath)
    $baseUri = [Uri]::new($base)
    $targetUri = [Uri]::new($target)
    [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace('/', '\')
}

$PythonPath = Resolve-KseniaPython -ProjectRoot $projectRoot -ExplicitPath $PythonPath
if (-not $PythonPath) {
    throw 'Python 3.12 для проверки архива не найден. Сначала установите среду Ксении.'
}
$stageRoot = Join-Path $DestinationRoot ('.ksenia-stage-' + [Guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $stageRoot $packageName
$verifyRoot = Join-Path $DestinationRoot ('.ksenia-verify-' + [Guid]::NewGuid().ToString('N'))
Assert-ChildPath $DestinationRoot $stageRoot
Assert-ChildPath $DestinationRoot $verifyRoot

$excludedFiles = @{}
foreach ($item in $manifest.package.excluded_files) {
    $excludedFiles[[string]$item.Replace('\', '/').Trim('/').ToLowerInvariant()] = $true
}
$excludedParts = @($manifest.package.excluded_path_parts | ForEach-Object {
    [string]$_.Replace('\', '/').Trim('/').ToLowerInvariant()
})
$forbiddenExtensions = @{}
foreach ($item in $manifest.package.forbidden_extensions) {
    $forbiddenExtensions[[string]$item.ToLowerInvariant()] = $true
}

function Test-ExcludedPath {
    param([string]$Relative)
    $normalized = $Relative.Replace('\', '/').Trim('/').ToLowerInvariant()
    if ($excludedFiles.ContainsKey($normalized)) { return $true }
    $parts = @($normalized -split '/')
    foreach ($forbidden in $excludedParts) {
        if ($forbidden.Contains('/')) {
            if ($normalized -eq $forbidden -or $normalized.StartsWith($forbidden + '/')) { return $true }
        } elseif ($parts -contains $forbidden) {
            return $true
        }
    }
    return $false
}

function Copy-PackageFile {
    param([string]$Source)
    $sourcePath = [IO.Path]::GetFullPath($Source)
    if (-not $sourcePath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Источник вне проекта: $sourcePath"
    }
    $relative = (Get-RelativePathCompat $projectRoot $sourcePath).Replace('\', '/')
    if (Test-ExcludedPath $relative) { return }
    $extension = [IO.Path]::GetExtension($sourcePath).ToLowerInvariant()
    if ($forbiddenExtensions.ContainsKey($extension)) {
        throw "Запрещённый файл в разрешённом дереве: $relative"
    }
    $target = Join-Path $packageRoot $relative.Replace('/', '\')
    Assert-ChildPath $packageRoot $target
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination $target
}

try {
    New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null
    foreach ($relative in $manifest.package.included_files) {
        $source = Join-Path $projectRoot ([string]$relative)
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Не найден файл для архива: $relative"
        }
        Copy-PackageFile $source
    }
    foreach ($rootName in $manifest.package.included_roots) {
        $sourceRoot = Join-Path $projectRoot ([string]$rootName)
        if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
            throw "Не найден каталог для архива: $rootName"
        }
        foreach ($source in Get-ChildItem -LiteralPath $sourceRoot -File -Recurse) {
            Copy-PackageFile $source.FullName
        }
    }
    foreach ($relative in $manifest.package.included_workspace_files) {
        $source = Join-Path $projectRoot ([string]$relative)
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Не найден стартовый файл workspace: $relative"
        }
        Copy-PackageFile $source
    }

    $entries = [Collections.Generic.List[object]]::new()
    foreach ($file in Get-ChildItem -LiteralPath $packageRoot -File -Recurse | Sort-Object FullName) {
        $relative = (Get-RelativePathCompat $packageRoot $file.FullName).Replace('\', '/')
        $entries.Add([ordered]@{
            path = $relative
            size_bytes = [int64]$file.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
        })
    }
    $packageManifest = [ordered]@{
        schema_version = 1
        project = [ordered]@{
            name = [string]$manifest.project.name
            version = $version
        }
        bundle = 'online'
        created_at = [DateTimeOffset]::Now.ToString('o')
        contains_model_weights = $false
        files = $entries.ToArray()
    }
    [IO.File]::WriteAllText(
        (Join-Path $packageRoot 'PACKAGE-MANIFEST.json'),
        (($packageManifest | ConvertTo-Json -Depth 10) + "`n"),
        $utf8
    )

    & $PythonPath (Join-Path $packageRoot 'scripts\validate_release.py') `
        --package-root $packageRoot --require-package
    if ($LASTEXITCODE -ne 0) { throw 'Staged tree не прошёл машинную проверку.' }

    $archivePath = Join-Path $DestinationRoot "$packageName-online.zip"
    if (Test-Path -LiteralPath $archivePath) {
        $historyRoot = Join-Path $DestinationRoot 'history'
        New-Item -ItemType Directory -Force -Path $historyRoot | Out-Null
        $historyPath = Join-Path $historyRoot ("$packageName-online-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')
        Assert-ChildPath $DestinationRoot $historyPath
        Move-Item -LiteralPath $archivePath -Destination $historyPath
        Write-Host "Предыдущий архив сохранён: $historyPath"
    }
    Compress-Archive -LiteralPath $packageRoot -DestinationPath $archivePath -CompressionLevel Optimal

    New-Item -ItemType Directory -Force -Path $verifyRoot | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $verifyRoot
    $verifiedPackageRoot = Join-Path $verifyRoot $packageName
    & $PythonPath (Join-Path $verifiedPackageRoot 'scripts\validate_release.py') `
        --package-root $verifiedPackageRoot --require-package
    if ($LASTEXITCODE -ne 0) { throw 'Созданный ZIP не прошёл повторную проверку.' }

    $archiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash
    Write-Host "Готов переносимый архив: $archivePath"
    Write-Host "SHA-256: $archiveHash"
}
finally {
    foreach ($temporary in @($stageRoot, $verifyRoot)) {
        if (-not (Test-Path -LiteralPath $temporary)) { continue }
        Assert-ChildPath $DestinationRoot $temporary
        if ((Split-Path -Leaf $temporary) -notlike '.ksenia-*') {
            throw "Отказ от удаления неоднозначного временного пути: $temporary"
        }
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
