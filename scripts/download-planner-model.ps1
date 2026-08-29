[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]*$')]
    [string]$Profile,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]*$')]
    [string]$Asset = ''
)

# Совместимый вход для старых ярлыков; вся логика теперь находится в общем
# загрузчике и определяется только декларативным каталогом моделей.
& (Join-Path $PSScriptRoot 'download-model-assets.ps1') @PSBoundParameters
exit $LASTEXITCODE
