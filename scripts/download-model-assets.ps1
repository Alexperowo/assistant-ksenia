[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]*$')]
    [string]$Profile,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]*$')]
    [string]$Asset = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$python = Resolve-KseniaPython -ProjectRoot $projectRoot
if (-not $python) {
    throw 'Python 3.12 не найден. Сначала выполните штатную установку Ксении.'
}

$arguments = @(
    (Join-Path $PSScriptRoot 'model-assets.py'),
    'download',
    $Profile
)
if ($Asset) { $arguments += @('--asset', $Asset) }
& $python @arguments
exit $LASTEXITCODE
