param(
    [int]$Port = 18765,
    [string]$Pin = '123456',
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$PythonPath = Resolve-KseniaPython -ProjectRoot $projectRoot -ExplicitPath $PythonPath
if (-not $PythonPath) { throw 'Python 3.12 для LAN-теста не найден.' }

function Test-LocalPort {
    param([int]$TargetPort)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.Connect('127.0.0.1', $TargetPort)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

if (Test-LocalPort -TargetPort $Port) {
    throw "Тестовый порт $Port уже занят неизвестным процессом; он не будет остановлен."
}

$previousDiagnosticsDisabled = $env:BUTLER_DIAGNOSTICS_DISABLED
$env:BUTLER_DIAGNOSTICS_DISABLED = '1'
$process = $null
try {
    $process = Start-Process -FilePath $PythonPath -ArgumentList @(
        '-m', 'butler', '--no-speech', 'lan', '--host', '127.0.0.1',
        '--port', [string]$Port, '--pin', $Pin
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru

    $baseUrl = "http://127.0.0.1:$Port"
    $page = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 200
        try {
            $page = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/" -TimeoutSec 2
            if ($page.StatusCode -eq 200) { break }
        }
        catch {}
    }
    if (-not $page -or $page.StatusCode -ne 200) {
        throw 'Тестовый LAN-сервер не запустился.'
    }

    $badPinStatus = 0
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/api/auth" -Method Post `
            -ContentType 'application/json' -Body '{"pin":"000000"}' -TimeoutSec 3 | Out-Null
    }
    catch {
        $badPinStatus = [int]$_.Exception.Response.StatusCode
    }
    $good = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/api/auth" -Method Post `
        -ContentType 'application/json' -Body ("{`"pin`":`"$Pin`"}") -TimeoutSec 3

    $result = [pscustomobject]@{
        PageStatus = $page.StatusCode
        HasRussianTitle = $page.Content -match 'Ксения'
        HasKeyboardHelp = $page.Content -match 'Control\+Enter'
        BadPinStatus = $badPinStatus
        GoodPinStatus = $good.StatusCode
    }
    if (
        $result.PageStatus -ne 200 -or
        -not $result.HasRussianTitle -or
        -not $result.HasKeyboardHelp -or
        $result.BadPinStatus -ne 401 -or
        $result.GoodPinStatus -ne 200
    ) {
        throw "LAN-проверка вернула неверный результат: $($result | ConvertTo-Json -Compress)"
    }
    $result | ConvertTo-Json -Compress
}
finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        Wait-Process -Id $process.Id -Timeout 5 -ErrorAction SilentlyContinue
    }
    for ($attempt = 0; $attempt -lt 30 -and (Test-LocalPort -TargetPort $Port); $attempt++) {
        Start-Sleep -Milliseconds 100
    }
    $portStillOpen = Test-LocalPort -TargetPort $Port
    if ($null -eq $previousDiagnosticsDisabled) {
        Remove-Item Env:BUTLER_DIAGNOSTICS_DISABLED -ErrorAction SilentlyContinue
    }
    else {
        $env:BUTLER_DIAGNOSTICS_DISABLED = $previousDiagnosticsDisabled
    }
    if ($portStillOpen) {
        throw "Тестовый LAN-порт $Port не освободился после завершения дочернего процесса."
    }
}
