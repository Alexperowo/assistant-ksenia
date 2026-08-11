$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$auditDir = Join-Path $projectRoot 'runtime\audit'
$report = Join-Path $auditDir 'latest.txt'
New-Item -ItemType Directory -Force -Path $auditDir | Out-Null
$pythonCandidates = [System.Collections.Generic.List[string]]::new()
$userConfigPath = Join-Path $projectRoot 'config\user.json'
if (Test-Path -LiteralPath $userConfigPath) {
    try {
        $userConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $userConfigPath | ConvertFrom-Json
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
    'ОШИБКА: Python Ксении не найден. Запустите INSTALL.cmd.' | Tee-Object -FilePath $report
    exit 2
}

function Invoke-PythonCaptured {
    param([string[]]$Arguments)
    $stamp = [Guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $auditDir "$stamp.stdout.txt"
    $stderrPath = Join-Path $auditDir "$stamp.stderr.txt"
    try {
        $process = Start-Process -FilePath $python -ArgumentList $Arguments `
            -WorkingDirectory $projectRoot -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $lines = [System.Collections.Generic.List[string]]::new()
        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $path) {
                foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
                    $lines.Add([string]$line)
                }
            }
        }
        [pscustomobject]@{ Code = $process.ExitCode; Lines = $lines.ToArray() }
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $stdoutPath, $stderrPath
    }
}

$staticLines = [System.Collections.Generic.List[string]]::new()
$staticLines.Add('=== Статическая проверка проекта ===')
$staticCode = 0
try {
    foreach ($script in Get-ChildItem -LiteralPath (Join-Path $projectRoot 'scripts') -Filter '*.ps1') {
        $tokens = $null
        $parseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            $script.FullName, [ref]$tokens, [ref]$parseErrors
        ) | Out-Null
        if ($parseErrors.Count -gt 0) {
            throw "Ошибка PowerShell: $($script.Name): $($parseErrors[0].Message)"
        }
    }
    $staticLines.Add('[ГОТОВО] Синтаксис PowerShell')
    foreach ($directory in @('config', 'procedures')) {
        Get-ChildItem -LiteralPath (Join-Path $projectRoot $directory) -Filter '*.json' | ForEach-Object {
            Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName | ConvertFrom-Json | Out-Null
        }
    }
    $staticLines.Add('[ГОТОВО] Синтаксис JSON')
}
catch {
    $staticCode = 1
    $staticLines.Add("[ОШИБКА] $($_.Exception.Message)")
}
$staticLines.ToArray() | Tee-Object -FilePath $report

$compileResult = Invoke-PythonCaptured @(
    '-m', 'compileall', '-q',
    (Join-Path $projectRoot 'src'),
    (Join-Path $projectRoot 'scripts'),
    (Join-Path $projectRoot 'tests')
)
if ($compileResult.Code -eq 0) {
    '[ГОТОВО] Синтаксис Python' | Tee-Object -FilePath $report -Append
} else {
    '[ОШИБКА] Синтаксис Python' | Tee-Object -FilePath $report -Append
    $compileResult.Lines | Tee-Object -FilePath $report -Append
}

$testResult = Invoke-PythonCaptured @(
    (Join-Path $projectRoot 'scripts\run_test_suite.py'), '--order-audit'
)
$testOutput = $testResult.Lines
$testCode = $testResult.Code
$testOutput | Tee-Object -FilePath $report -Append

$releaseResult = Invoke-PythonCaptured @(
    (Join-Path $projectRoot 'scripts\validate_release.py'),
    '--package-root', $projectRoot
)
$releaseResult.Lines | Tee-Object -FilePath $report -Append

$packageResult = Invoke-PythonCaptured @('-m', 'pip', 'check')
$packageResult.Lines | Tee-Object -FilePath $report -Append

$doctorArguments = @('-m', 'butler', '--no-speech', 'doctor')
if ($env:KSENIA_INSTALLATION_MODE -eq '1') { $doctorArguments += '--installation-mode' }
$doctorResult = Invoke-PythonCaptured $doctorArguments
$doctorOutput = $doctorResult.Lines
$doctorCode = $doctorResult.Code
$doctorOutput | Tee-Object -FilePath $report -Append
if ($staticCode -ne 0) { exit $staticCode }
if ($compileResult.Code -ne 0) { exit $compileResult.Code }
if ($testCode -ne 0) { exit $testCode }
if ($releaseResult.Code -ne 0) { exit $releaseResult.Code }
if ($packageResult.Code -ne 0) { exit $packageResult.Code }
if ($doctorCode -ne 0) { exit $doctorCode }

$diagnosticsResult = Invoke-PythonCaptured @('-m', 'butler', '--no-speech', 'diagnostics')
$diagnosticsResult.Lines | Tee-Object -FilePath $report -Append
if ($diagnosticsResult.Code -ne 0) { exit $diagnosticsResult.Code }

$modelResult = Invoke-PythonCaptured @((Join-Path $projectRoot 'scripts\test_active_model.py'))
$modelResult.Lines | Tee-Object -FilePath $report -Append
if ($modelResult.Code -ne 0) { exit $modelResult.Code }

$ragResult = Invoke-PythonCaptured @((Join-Path $projectRoot 'scripts\test-rag.py'))
$ragResult.Lines | Tee-Object -FilePath $report -Append
if ($ragResult.Code -ne 0) { exit $ragResult.Code }

try {
    & (Join-Path $projectRoot 'scripts\test-lan.ps1') -PythonPath $python |
        Tee-Object -FilePath $report -Append
}
catch {
    "[ОШИБКА] LAN: $($_.Exception.Message)" | Tee-Object -FilePath $report -Append
    exit 1
}

$speechResult = Invoke-PythonCaptured @((Join-Path $projectRoot 'scripts\test_speech_models.py'))
$speechResult.Lines | Tee-Object -FilePath $report -Append
exit $speechResult.Code
