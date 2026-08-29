$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $projectRoot 'runtime\lan-browser-test.pid'
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$python = Resolve-KseniaPython -ProjectRoot $projectRoot
if (-not $python) { throw 'Python 3.12 для LAN-теста не найден.' }
$process = Start-Process -FilePath $python -ArgumentList @(
    '-m', 'butler', '--no-speech', 'lan', '--host', '127.0.0.1',
    '--port', '18766', '--pin', '123456'
) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
[IO.File]::WriteAllText($pidPath, [string]$process.Id, [Text.Encoding]::ASCII)
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 200
    try {
        $page = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18766/' -TimeoutSec 2
        if ($page.StatusCode -eq 200) {
            Write-Output "PID=$($process.Id) STATUS=200"
            exit 0
        }
    }
    catch {}
}
Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $pidPath
throw 'Тестовый LAN-сервер не запустился.'
