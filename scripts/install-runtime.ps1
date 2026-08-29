param(
    [string]$InstallRoot = '',
    [string]$ModelStorageRoot = '',
    [string]$ProjectRoot = '',
    [switch]$SkipAudit,
    [switch]$SkipShortcuts,
    [switch]$SkipLlama,
    [switch]$SkipSpeechModels,
    [switch]$RefreshBrowser
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

$projectRoot = if ($ProjectRoot) { [IO.Path]::GetFullPath($ProjectRoot) } else { Split-Path -Parent $PSScriptRoot }
$defaultConfigPath = Join-Path $projectRoot 'config\default.json'
$userConfigPath = Join-Path $projectRoot 'config\user.json'
$assetLockPath = Join-Path $projectRoot 'config\runtime-assets.lock.json'
$runtimeRequirementsPath = Join-Path $projectRoot 'requirements\runtime.lock.txt'
$torchRequirementsPath = Join-Path $projectRoot 'requirements\torch-cu128.lock.txt'
$defaultConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $defaultConfigPath | ConvertFrom-Json
$assetLock = Get-Content -Raw -Encoding UTF8 -LiteralPath $assetLockPath | ConvertFrom-Json

if (-not $InstallRoot) {
    if (-not $env:LocalAppData) {
        throw 'Не определён LocalAppData; передайте каталог явно через -InstallRoot.'
    }
    $InstallRoot = Join-Path $env:LocalAppData 'Ksenia\Butler'
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$pythonVersion = [string]$assetLock.python.version
$pythonRoot = Join-Path $InstallRoot 'Python312'
$defaultVenvRoot = Join-Path $InstallRoot 'venv'
$downloadRoot = Join-Path $InstallRoot 'downloads'
$installer = Join-Path $downloadRoot "python-$pythonVersion-amd64.exe"
$installerUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-amd64.exe"

function Test-Python {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $actual = (& $Path -c "import platform; print(platform.python_version())" 2>$null | Select-Object -Last 1).Trim()
        return $LASTEXITCODE -eq 0 -and $actual -eq $pythonVersion
    }
    catch { return $false }
}

function Get-EffectiveValue {
    param(
        [object]$User,
        [object]$Default,
        [string]$Section,
        [string]$Name,
        [object]$Fallback = $null
    )
    $userSection = $User.PSObject.Properties[$Section].Value
    if ($userSection -and $userSection.PSObject.Properties[$Name]) {
        return $userSection.PSObject.Properties[$Name].Value
    }
    $defaultSection = $Default.PSObject.Properties[$Section].Value
    if ($defaultSection -and $defaultSection.PSObject.Properties[$Name]) {
        return $defaultSection.PSObject.Properties[$Name].Value
    }
    return $Fallback
}

function Ensure-Section {
    param([object]$Config, [string]$Name)
    if (-not $Config.PSObject.Properties[$Name]) {
        $Config | Add-Member -NotePropertyName $Name -NotePropertyValue ([pscustomobject]@{})
    }
    return $Config.PSObject.Properties[$Name].Value
}

function Set-Property {
    param([object]$Object, [string]$Name, [object]$Value)
    if ($Object.PSObject.Properties[$Name]) {
        $Object.PSObject.Properties[$Name].Value = $Value
    } else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Resolve-ProjectPath {
    param([string]$Value)
    if (-not $Value) { return $null }
    if ([IO.Path]::IsPathRooted($Value)) { return [IO.Path]::GetFullPath($Value) }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $Value))
}

function Test-RuntimePackages {
    param([string]$Path)
    if (-not (Test-Python $Path)) { return $false }
    $checks = [Collections.Generic.List[string]]::new()
    $checks.Add("assert m.version('pip') == '$([string]$assetLock.python.pip)'")
    foreach ($lockFile in @($torchRequirementsPath, $runtimeRequirementsPath)) {
        foreach ($raw in Get-Content -LiteralPath $lockFile -Encoding UTF8) {
            $line = $raw.Trim()
            if (-not $line -or $line.StartsWith('#')) { continue }
            $parts = $line.Split(@('=='), 2, [StringSplitOptions]::None)
            if ($parts.Count -ne 2) { return $false }
            $checks.Add("assert m.version('$($parts[0])') == '$($parts[1])'")
        }
    }
    # Import smoke tests belong to Doctor. Version reconciliation must not fail
    # because a valid library writes a harmless message to native stderr.
    $code = "import importlib.metadata as m; " + ($checks.ToArray() -join '; ')
    try {
        & $Path -c $code *> $null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

function Test-WhisperModel {
    param([string]$Path, [switch]$FullHash)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    foreach ($property in $assetLock.whisper.files.PSObject.Properties) {
        $file = Join-Path $Path $property.Name
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return $false }
        if ((Get-Item -LiteralPath $file).Length -ne [int64]$property.Value.size_bytes) { return $false }
        if ($FullHash -and (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash -ne [string]$property.Value.sha256) {
            return $false
        }
    }
    return $true
}

function Move-ToInstallerQuarantine {
    param([string]$Path, [string]$AllowedRoot)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $resolvedRoot = [IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\')
    if (-not $resolvedPath.StartsWith($resolvedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Отказ от перемещения пути вне управляемого каталога: $resolvedPath"
    }
    $quarantine = Join-Path $downloadRoot 'quarantine'
    New-Item -ItemType Directory -Force -Path $quarantine | Out-Null
    $leaf = Split-Path -Leaf $Path
    $destination = Join-Path $quarantine ("$leaf.invalid-" + [Guid]::NewGuid().ToString('N'))
    Move-Item -LiteralPath $resolvedPath -Destination $destination
    Write-Warning "Повреждённый файл изолирован: $destination"
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $userConfigPath) | Out-Null
if (Test-Path -LiteralPath $userConfigPath) {
    $config = Get-Content -Raw -Encoding UTF8 -LiteralPath $userConfigPath | ConvertFrom-Json
} else {
    $config = [pscustomobject]@{}
    [IO.File]::WriteAllText($userConfigPath, "{}`n", $utf8)
    Write-Host 'Создан личный файл настроек config\user.json.'
}

$runtimeCandidates = [Collections.Generic.List[string]]::new()
$configuredPython = [string](Get-EffectiveValue $config $defaultConfig 'voice' 'python' '')
if ($configuredPython) { $runtimeCandidates.Add($configuredPython) }
if ($env:KSENIA_PYTHON) { $runtimeCandidates.Add([string]$env:KSENIA_PYTHON) }
if ($env:VIRTUAL_ENV) {
    $runtimeCandidates.Add((Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'))
}
$runtimeCandidates.Add((Join-Path $projectRoot '.venv\Scripts\python.exe'))
$runtimeCandidates.Add((Join-Path $projectRoot 'venv\Scripts\python.exe'))
$runtimeCandidates.Add((Join-Path $defaultVenvRoot 'Scripts\python.exe'))
$venvPython = $runtimeCandidates | Where-Object { Test-Python $_ } | Select-Object -First 1

if ($venvPython) {
    Write-Host "Использую существующую среду Python: $venvPython"
} else {
    $baseCandidates = @(
        (Join-Path $env:LocalAppData 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe'),
        (Join-Path $pythonRoot 'python.exe')
    )
    $python = $baseCandidates | Where-Object { Test-Python $_ } | Select-Object -First 1
    if (-not $python) {
        New-Item -ItemType Directory -Force -Path $InstallRoot, $downloadRoot | Out-Null
        if (-not (Test-Path -LiteralPath $installer)) {
            Write-Host "Python $pythonVersion не найден. Скачиваю официальный установщик..."
            try {
                Start-BitsTransfer -Source $installerUrl -Destination $installer
            }
            catch {
                Invoke-WebRequest -UseBasicParsing -Uri $installerUrl -OutFile $installer -TimeoutSec 900
            }
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
            throw 'Цифровая подпись установщика Python не прошла проверку.'
        }
        Write-Host "Устанавливаю отдельный Python Ксении в $pythonRoot..."
        $process = Start-Process -FilePath $installer -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0',
            'Include_test=0', "TargetDir=$pythonRoot"
        ) -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Установщик Python завершился с кодом $($process.ExitCode)." }
        $python = Join-Path $pythonRoot 'python.exe'
    }

    $venvRoot = $defaultVenvRoot
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    if (-not (Test-Python $venvPython)) {
        New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
        & $python -m venv --clear $venvRoot
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать отдельную среду Python.' }
    }
}

if (Test-RuntimePackages $venvPython) {
    Write-Host 'Все закреплённые библиотеки Ксении уже установлены.'
} else {
    Write-Host 'Устанавливаю проверенные версии библиотек Ксении...'
    & $venvPython -m pip install "pip==$([string]$assetLock.python.pip)"
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить закреплённую версию pip.' }
    & $venvPython -m pip install --upgrade --index-url https://download.pytorch.org/whl/cu128 `
        --requirement $torchRequirementsPath
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить проверенную CUDA-версию Torch.' }
    & $venvPython -m pip install --upgrade --requirement $runtimeRequirementsPath
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить закреплённые библиотеки Ксении.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'pip check обнаружил конфликт библиотек.' }
    if (-not (Test-RuntimePackages $venvPython)) {
        throw 'Версии установленных библиотек не совпали с runtime-assets.lock.json.'
    }
}

$llamaServer = Join-Path $projectRoot 'tools\llama.cpp\llama-server.exe'
if (-not $SkipLlama -and -not (Test-Path -LiteralPath $llamaServer -PathType Leaf)) {
    Write-Host 'Устанавливаю закреплённую сборку llama.cpp...'
    & (Join-Path $PSScriptRoot 'install-llama.ps1')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $llamaServer -PathType Leaf)) {
        throw 'Не удалось установить llama.cpp.'
    }
}

$voice = Ensure-Section $config 'voice'
$browser = Ensure-Section $config 'browser'
$paths = Ensure-Section $config 'paths'
Set-Property $voice 'python' $venvPython
Set-Property $browser 'python' $venvPython

$configuredBrowser = [string](Get-EffectiveValue $config $defaultConfig 'browser' 'executable' '')
if ($RefreshBrowser -or -not $configuredBrowser -or -not (Test-Path -LiteralPath $configuredBrowser -PathType Leaf)) {
    $browserRoot = Join-Path $InstallRoot 'browsers'
    New-Item -ItemType Directory -Force -Path $browserRoot | Out-Null
    Write-Host 'Устанавливаю закреплённый Chromium для Ксении...'
    $previousBrowserPath = $env:PLAYWRIGHT_BROWSERS_PATH
    try {
        $env:PLAYWRIGHT_BROWSERS_PATH = $browserRoot
        & $venvPython -m playwright install --no-shell chromium
        if ($LASTEXITCODE -ne 0) { throw 'Playwright не смог установить Chromium.' }
        $browserCandidates = @(
            Get-ChildItem -LiteralPath $browserRoot -Directory -Filter 'chromium-*' |
                ForEach-Object {
                    $candidate = Join-Path $_.FullName 'chrome-win64\chrome.exe'
                    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate }
                }
        )
        $browserCandidate = $browserCandidates | Sort-Object -Descending | Select-Object -First 1
        if (-not $browserCandidate) { throw 'Не удалось найти chrome.exe после установки Chromium.' }
        $configuredBrowser = [IO.Path]::GetFullPath($browserCandidate)
    }
    finally {
        $env:PLAYWRIGHT_BROWSERS_PATH = $previousBrowserPath
    }
}
if (-not (Test-Path -LiteralPath $configuredBrowser -PathType Leaf)) {
    throw "Chromium не найден после установки: $configuredBrowser"
}
Set-Property $browser 'executable' ([IO.Path]::GetFullPath($configuredBrowser))
$profileDir = [string](Get-EffectiveValue $config $defaultConfig 'browser' 'profile_dir' '')
if (-not $profileDir -or (-not (Test-Path -LiteralPath (Split-Path -Parent $profileDir)))) {
    $profileDir = Join-Path $InstallRoot 'browser-profile'
}
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
Set-Property $browser 'profile_dir' ([IO.Path]::GetFullPath($profileDir))

$wakeModelRaw = [string](Get-EffectiveValue $config $defaultConfig 'voice' 'wake_model' 'runtime/voice/vosk-model-small-ru-0.22')
$wakeModelPath = Resolve-ProjectPath $wakeModelRaw
$wakeReady = $wakeModelPath -and (Test-Path -LiteralPath (Join-Path $wakeModelPath 'conf\model.conf') -PathType Leaf)
$voiceRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'runtime\voice'))
$wakeModelInsideManagedRoot = $wakeModelPath -and [IO.Path]::GetFullPath($wakeModelPath).StartsWith(
    $voiceRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase
)
if (-not $wakeReady -and -not $wakeModelInsideManagedRoot) {
    Write-Warning 'Настроенная внешняя wake-модель не готова и оставлена без изменений; установка продолжится в управляемый runtime.'
    $wakeModelRaw = "runtime/voice/$([string]$assetLock.vosk.directory)"
    $wakeModelPath = [IO.Path]::GetFullPath((Join-Path $projectRoot $wakeModelRaw))
    $wakeReady = Test-Path -LiteralPath (Join-Path $wakeModelPath 'conf\model.conf') -PathType Leaf
    $wakeModelInsideManagedRoot = $true
}
if (-not $wakeReady -and -not $SkipSpeechModels) {
    New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null
    $voskArchive = Join-Path $downloadRoot ([string]$assetLock.vosk.archive)
    if (Test-Path -LiteralPath $voskArchive) {
        $archiveValid = (Get-Item -LiteralPath $voskArchive).Length -eq [int64]$assetLock.vosk.size_bytes -and
            (Get-FileHash -Algorithm SHA256 -LiteralPath $voskArchive).Hash -eq [string]$assetLock.vosk.sha256
        if (-not $archiveValid) { Move-ToInstallerQuarantine $voskArchive -AllowedRoot $downloadRoot }
    }
    if (-not (Test-Path -LiteralPath $voskArchive)) {
        Write-Host 'Скачиваю компактную русскую модель голосовой активации Vosk...'
        $partial = "$voskArchive.partial"
        if (Test-Path -LiteralPath $partial) { Move-ToInstallerQuarantine $partial -AllowedRoot $downloadRoot }
        try {
            Start-BitsTransfer -Source ([string]$assetLock.vosk.url) -Destination $partial
        }
        catch {
            Invoke-WebRequest -UseBasicParsing -Uri ([string]$assetLock.vosk.url) -OutFile $partial -TimeoutSec 900
        }
        if ((Get-Item -LiteralPath $partial).Length -ne [int64]$assetLock.vosk.size_bytes -or
            (Get-FileHash -Algorithm SHA256 -LiteralPath $partial).Hash -ne [string]$assetLock.vosk.sha256) {
            Move-ToInstallerQuarantine $partial -AllowedRoot $downloadRoot
            throw 'Контрольная сумма Vosk не совпала.'
        }
        Move-Item -LiteralPath $partial -Destination $voskArchive
    }
    $extractRoot = Join-Path $voiceRoot ('.vosk-install-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
    try {
        Expand-Archive -LiteralPath $voskArchive -DestinationPath $extractRoot
        $extracted = Join-Path $extractRoot ([string]$assetLock.vosk.directory)
        if (-not (Test-Path -LiteralPath (Join-Path $extracted 'conf\model.conf') -PathType Leaf)) {
            throw 'Архив Vosk не содержит ожидаемую структуру модели.'
        }
        if (Test-Path -LiteralPath $wakeModelPath) {
            Move-ToInstallerQuarantine $wakeModelPath -AllowedRoot $voiceRoot
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $wakeModelPath) | Out-Null
        Move-Item -LiteralPath $extracted -Destination $wakeModelPath
    }
    finally {
        if (Test-Path -LiteralPath $extractRoot) { Remove-Item -LiteralPath $extractRoot -Recurse -Force }
    }
}
Set-Property $voice 'wake_model' $wakeModelRaw

$configuredModels = [string](Get-EffectiveValue $config $defaultConfig 'paths' 'models_dir' '')
$configuredModelsIsUsable = $configuredModels -and
    [IO.Path]::IsPathRooted($configuredModels) -and
    (Test-Path -LiteralPath ([IO.Path]::GetPathRoot($configuredModels)) -PathType Container)
$modelsRoot = if ($ModelStorageRoot) {
    [IO.Path]::GetFullPath($ModelStorageRoot)
} elseif ($configuredModelsIsUsable) {
    [IO.Path]::GetFullPath($configuredModels)
} else {
    Join-Path $InstallRoot 'Models'
}
New-Item -ItemType Directory -Force -Path $modelsRoot | Out-Null
Set-Property $paths 'models_dir' $modelsRoot

$speechModelsRoot = Join-Path $modelsRoot 'Speech'
$ttsModelPath = Join-Path $speechModelsRoot ([string]$assetLock.silero_tts.filename)
$ttsModelValid = (Test-Path -LiteralPath $ttsModelPath -PathType Leaf) -and
    (Get-Item -LiteralPath $ttsModelPath).Length -eq [int64]$assetLock.silero_tts.size_bytes -and
    (Get-FileHash -Algorithm SHA256 -LiteralPath $ttsModelPath).Hash -eq [string]$assetLock.silero_tts.sha256
if ((Test-Path -LiteralPath $ttsModelPath) -and -not $ttsModelValid) {
    Move-ToInstallerQuarantine $ttsModelPath -AllowedRoot $modelsRoot
}
if (-not $ttsModelValid -and -not $SkipSpeechModels) {
    New-Item -ItemType Directory -Force -Path $speechModelsRoot, $downloadRoot | Out-Null
    $ttsPartial = Join-Path $downloadRoot (([string]$assetLock.silero_tts.filename) + '.partial')
    if (Test-Path -LiteralPath $ttsPartial) {
        Move-ToInstallerQuarantine $ttsPartial -AllowedRoot $downloadRoot
    }
    Write-Host 'Скачиваю закреплённый локальный голос Silero...'
    try {
        Start-BitsTransfer -Source ([string]$assetLock.silero_tts.url) -Destination $ttsPartial
    }
    catch {
        Invoke-WebRequest -UseBasicParsing -Uri ([string]$assetLock.silero_tts.url) -OutFile $ttsPartial -TimeoutSec 900
    }
    if ((Get-Item -LiteralPath $ttsPartial).Length -ne [int64]$assetLock.silero_tts.size_bytes -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $ttsPartial).Hash -ne [string]$assetLock.silero_tts.sha256) {
        Move-ToInstallerQuarantine $ttsPartial -AllowedRoot $downloadRoot
        throw 'Контрольная сумма Silero TTS не совпала.'
    }
    Move-Item -LiteralPath $ttsPartial -Destination $ttsModelPath
    $ttsModelValid = $true
}
if ($ttsModelValid) {
    Set-Property $voice 'tts_model_path' ([IO.Path]::GetFullPath($ttsModelPath))
    Set-Property $voice 'tts_model_expected_size_bytes' ([int64]$assetLock.silero_tts.size_bytes)
    Set-Property $voice 'tts_model_sha256' ([string]$assetLock.silero_tts.sha256)
}

$configuredWhisper = [string](Get-EffectiveValue $config $defaultConfig 'voice' 'stt_model' '')
$whisperPath = if (Test-WhisperModel $configuredWhisper) {
    [IO.Path]::GetFullPath($configuredWhisper)
} else {
    Join-Path (Join-Path $modelsRoot 'Speech') ([string]$assetLock.whisper.directory)
}
if (-not (Test-WhisperModel $whisperPath) -and -not $SkipSpeechModels) {
    New-Item -ItemType Directory -Force -Path $whisperPath | Out-Null
    Write-Host 'Скачиваю закреплённую модель Whisper Large v3 Turbo. Это около 1,6 ГБ...'
    $env:KSENIA_HF_REPO = [string]$assetLock.whisper.repo
    $env:KSENIA_HF_REVISION = [string]$assetLock.whisper.revision
    $env:KSENIA_HF_DESTINATION = $whisperPath
    $env:HF_XET_HIGH_PERFORMANCE = '0'
    $env:HF_XET_FIXED_DOWNLOAD_CONCURRENCY = '4'
    $env:HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY = '0'
    $downloadCode = @'
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id=os.environ["KSENIA_HF_REPO"],
    revision=os.environ["KSENIA_HF_REVISION"],
    local_dir=os.environ["KSENIA_HF_DESTINATION"],
    allow_patterns=["config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"],
    max_workers=2,
)
'@
    $downloadCode | & $venvPython -
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось скачать Whisper.' }
}
if (-not $SkipSpeechModels -and -not (Test-WhisperModel $whisperPath -FullHash)) {
    throw 'Размер или SHA-256 файлов Whisper не совпал с runtime-assets.lock.json.'
}
if (Test-WhisperModel $whisperPath) {
    Set-Property $voice 'stt_model' ([IO.Path]::GetFullPath($whisperPath))
} elseif ($SkipSpeechModels) {
    Write-Host 'Веса Vosk/Whisper пропущены; их нужно установить отдельным model pack.'
}

$temporary = Join-Path (Split-Path -Parent $userConfigPath) ('.' + (Split-Path -Leaf $userConfigPath) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
$json = $config | ConvertTo-Json -Depth 30
try {
    [IO.File]::WriteAllText($temporary, $json + "`n", $utf8)
    Move-Item -Force -LiteralPath $temporary -Destination $userConfigPath
}
finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -Force -LiteralPath $temporary -ErrorAction SilentlyContinue
    }
}

try { & (Join-Path $PSScriptRoot 'hardware-report.ps1') | Out-Host } catch { Write-Warning $_.Exception.Message }
if (-not $SkipShortcuts) { & (Join-Path $PSScriptRoot 'install-shortcuts.ps1') }
if ($SkipAudit) {
    Write-Host 'Среда Ксении приведена к закреплённым версиям. Полный аудит пропущен параметром.'
    exit 0
}
Write-Host 'Среда и голосовые модули Ксении проверены. Запускаю аудит...'
$previousInstallationMode = $env:KSENIA_INSTALLATION_MODE
try {
    $env:KSENIA_INSTALLATION_MODE = '1'
    & (Join-Path $PSScriptRoot 'check.ps1')
    $auditCode = $LASTEXITCODE
}
finally {
    $env:KSENIA_INSTALLATION_MODE = $previousInstallationMode
}
exit $auditCode
