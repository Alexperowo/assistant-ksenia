param(
    [string]$SourceUrl = 'https://huggingface.co/OS-Software/gemma-4-26B-A4B-it-qat-q4_0-heretic-ja-GGUF/resolve/main/gemma-4-26B-A4B-it-qat-q4_0-heretic-ja-Q4_0.gguf',
    [string]$Destination = '',
    [long]$ExpectedBytes = 14249046720,
    [string]$ExpectedSha256 = '8ae92ac96260197a46042e7c6bc1ff996030aec7f73144fdcd23b7784737d04c'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$modelFileName = 'gemma-4-26B-A4B-it-qat-q4_0-heretic-ja-Q4_0.gguf'
if (-not $Destination) {
    $userConfigPath = Join-Path $projectRoot 'config\user.json'
    $configuredModels = ''
    if (Test-Path -LiteralPath $userConfigPath -PathType Leaf) {
        try {
            $userConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $userConfigPath | ConvertFrom-Json
            $configuredModels = [string]$userConfig.paths.models_dir
        }
        catch {}
    }
    $modelsRoot = if ($configuredModels -and [IO.Path]::IsPathRooted($configuredModels)) {
        [IO.Path]::GetFullPath($configuredModels)
    } elseif (Test-Path -LiteralPath 'D:\' -PathType Container) {
        'D:\AI\Models'
    } else {
        Join-Path $env:LocalAppData 'Ksenia\Models'
    }
    $Destination = Join-Path $modelsRoot $modelFileName
}
$Destination = [IO.Path]::GetFullPath($Destination)
$runtimeDir = Join-Path $projectRoot 'runtime\downloads'
$logPath = Join-Path $runtimeDir 'planner-model.log'
$readyPath = Join-Path $runtimeDir 'planner-model.ready.json'
$partial = "$Destination.partial"

function Write-DownloadLog {
    param([string]$Message)
    $line = "[$([DateTime]::Now.ToString('s'))] $Message"
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Force -Path $runtimeDir, (Split-Path -Parent $Destination) | Out-Null
    Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $readyPath

    if (Test-Path -LiteralPath $Destination) {
        $existing = Get-Item -LiteralPath $Destination
        if ($existing.Length -ne $ExpectedBytes) {
            throw "Конечный файл уже существует, но имеет неверный размер: $($existing.Length)."
        }
        Write-DownloadLog 'Файл уже имеет ожидаемый размер; проверяю SHA-256.'
    }
    else {
        $resumeBytes = 0
        if (Test-Path -LiteralPath $partial) {
            $resumeBytes = (Get-Item -LiteralPath $partial).Length
        }
        Write-DownloadLog "Начинаю или продолжаю загрузку с позиции $resumeBytes байт."
        & curl.exe --location --fail --retry 20 --retry-delay 5 --retry-all-errors `
            --continue-at - --output $partial $SourceUrl
        if ($LASTEXITCODE -ne 0) {
            throw "curl завершился с кодом $LASTEXITCODE. Частичный файл сохранён для продолжения."
        }
        $downloaded = Get-Item -LiteralPath $partial
        if ($downloaded.Length -ne $ExpectedBytes) {
            throw "Получен неверный размер: $($downloaded.Length), ожидалось $ExpectedBytes."
        }
        Move-Item -LiteralPath $partial -Destination $Destination
        Write-DownloadLog 'Размер совпал; загрузка завершена.'
    }

    $hash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "SHA-256 не совпал: $hash."
    }
    $ready = [ordered]@{
        path = $Destination
        bytes = $ExpectedBytes
        sha256 = $hash
        verified_at = [DateTimeOffset]::Now.ToString('o')
    } | ConvertTo-Json
    [IO.File]::WriteAllText($readyPath, $ready, [Text.UTF8Encoding]::new($false))
    Write-DownloadLog 'SHA-256 совпал. Модель готова к тестированию.'
    exit 0
}
catch {
    Write-DownloadLog "ОШИБКА: $($_.Exception.Message)"
    exit 1
}
