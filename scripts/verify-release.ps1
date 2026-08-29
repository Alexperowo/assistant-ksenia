param(
    [Parameter(Mandatory = $true)]
    [string]$ArchivePath,
    [string]$PythonPath = '',
    [string]$ExpectedSha256 = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$ArchivePath = [IO.Path]::GetFullPath($ArchivePath)
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Архив не найден: $ArchivePath"
}
$tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$temporaryRoot = Join-Path $tempParent ('ksenia-verify-' + [Guid]::NewGuid().ToString('N'))
if (-not $temporaryRoot.StartsWith($tempParent + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Небезопасный временный путь.'
}

try {
    $python = Resolve-KseniaPython -ProjectRoot $projectRoot -ExplicitPath $PythonPath
    if (-not $python) {
        throw 'Python 3.12 не найден. Сначала установите среду или передайте -PythonPath.'
    }
    $trustedValidator = Join-Path $PSScriptRoot 'validate_release.py'
    if (-not (Test-Path -LiteralPath $trustedValidator -PathType Leaf)) {
        throw 'Доверенный локальный валидатор релиза не найден.'
    }
    $validatorArguments = @(
        $trustedValidator,
        '--archive', $ArchivePath,
        '--extract-to', $temporaryRoot
    )
    if ($ExpectedSha256) {
        $validatorArguments += @('--expected-sha256', $ExpectedSha256)
    }
    else {
        Write-Warning 'Происхождение архива не подтверждено: передайте опубликованный SHA-256 через -ExpectedSha256.'
    }
    & $python @validatorArguments
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
