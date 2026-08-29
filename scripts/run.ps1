param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ButlerArgs
)

$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$python = Resolve-KseniaPython -ProjectRoot $projectRoot
if (-not $python) {
    try {
        & (Join-Path $PSScriptRoot 'speak.ps1') -Text (
            'Голосовая среда Ксении не найдена. Запустите ярлык Ксения — полный аудит.'
        )
    }
    catch {}
    throw 'Python Ксении не найден. Запустите INSTALL.cmd один раз.'
}

if ($ButlerArgs.Count -eq 0) {
    $ButlerArgs = @('menu')
}

& $python -m butler @ButlerArgs
exit $LASTEXITCODE
