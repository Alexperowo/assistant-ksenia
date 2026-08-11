param(
    [Parameter(Mandatory = $true)]
    [string]$ArchivePath,
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$ArchivePath = [IO.Path]::GetFullPath($ArchivePath)
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Архив не найден: $ArchivePath"
}
$tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$temporaryRoot = Join-Path $tempParent ('ksenia-verify-' + [Guid]::NewGuid().ToString('N'))
if (-not $temporaryRoot.StartsWith($tempParent + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Небезопасный временный путь.'
}

function Find-Python {
    if ($PythonPath -and (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { return $PythonPath }
    foreach ($candidate in @(
        'D:\AI\Butler\venv\Scripts\python.exe',
        'C:\butler-venv\Scripts\python.exe',
        (Join-Path $env:LocalAppData 'Ksenia\Butler\venv\Scripts\python.exe')
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw 'Python 3.12 не найден. Сначала установите среду или передайте -PythonPath.'
}

try {
    New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $temporaryRoot
    $packageManifests = @(Get-ChildItem -LiteralPath $temporaryRoot -Filter 'PACKAGE-MANIFEST.json' -File -Recurse)
    if ($packageManifests.Count -ne 1) {
        throw "Ожидался один PACKAGE-MANIFEST.json, найдено: $($packageManifests.Count)"
    }
    $packageRoot = Split-Path -Parent $packageManifests[0].FullName
    $python = Find-Python
    & $python (Join-Path $packageRoot 'scripts\validate_release.py') `
        --package-root $packageRoot --require-package
    if ($LASTEXITCODE -ne 0) { throw 'Архив не прошёл проверку.' }
    Write-Host "Архив корректен: $ArchivePath"
    Write-Host "SHA-256 ZIP: $((Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash)"
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        if ((Split-Path -Leaf $temporaryRoot) -notlike 'ksenia-verify-*') {
            throw "Отказ от удаления неоднозначного пути: $temporaryRoot"
        }
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
