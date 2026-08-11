param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ButlerArgs
)

$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'

$pythonCandidates = [System.Collections.Generic.List[string]]::new()
$userConfigPath = Join-Path $projectRoot 'config\user.json'
if (Test-Path -LiteralPath $userConfigPath) {
    try {
        $userConfig = Get-Content -Raw -LiteralPath $userConfigPath | ConvertFrom-Json
        if ($userConfig.voice.python) { $pythonCandidates.Add([string]$userConfig.voice.python) }
    }
    catch {}
}
$pythonCandidates.Add('D:\AI\Butler\venv\Scripts\python.exe')
$pythonCandidates.Add('C:\butler-venv\Scripts\python.exe')
$pythonCandidates.Add((Join-Path $env:LocalAppData 'Ksenia\Butler\venv\Scripts\python.exe'))
$python = $null
foreach ($candidate in $pythonCandidates) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    try {
        & $candidate --version *> $null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    }
    catch { continue }
}
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
