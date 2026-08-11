$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $projectRoot 'runtime\lan-browser-test.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { exit 0 }
$testPid = [int](Get-Content -Raw -LiteralPath $pidPath)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$testPid"
if ($process -and [string]$process.CommandLine -match '--port\s+18766') {
    Stop-Process -Id $testPid -Force
}
Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $pidPath
