param(
    [string]$OutputPath = '',
    [string]$ProjectRoot = ''
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$profilesPath = Join-Path $ProjectRoot 'config\hardware-profiles.json'
$profilesDocument = Get-Content -Raw -Encoding UTF8 -LiteralPath $profilesPath | ConvertFrom-Json

$computer = Get-CimInstance Win32_ComputerSystem
$operatingSystem = Get-CimInstance Win32_OperatingSystem
$processor = Get-CimInstance Win32_Processor | Select-Object -First 1
$ramGb = [Math]::Round([double]$computer.TotalPhysicalMemory / 1GB, 1)
$gpuName = $null
$vramMb = 0
$driverVersion = $null
$gpuSource = $null
$nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if ($nvidiaSmi) {
    try {
        $nvidiaPath = [string]$nvidiaSmi.Source
        $nvidiaLines = @(& $nvidiaPath --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>$null)
        $line = $nvidiaLines | Select-Object -First 1
        if ($line) {
            $parts = @($line -split ',' | ForEach-Object { $_.Trim() })
            if ($parts.Count -ge 3) {
                $gpuName = $parts[0]
                $vramMb = [int][double]$parts[1]
                $driverVersion = $parts[2]
                if ($vramMb -gt 0) { $gpuSource = 'nvidia-smi' }
            }
        }
    }
    catch { Write-Warning "nvidia-smi не удалось разобрать: $($_.Exception.Message)" }
}

if (-not $gpuName) {
    $display = Get-CimInstance Win32_VideoController | Sort-Object AdapterRAM -Descending | Select-Object -First 1
    if ($display) {
        $gpuName = [string]$display.Name
        if ($display.AdapterRAM) {
            $vramMb = [int][Math]::Round([double]$display.AdapterRAM / 1MB)
        }
        $driverVersion = [string]$display.DriverVersion
        $gpuSource = 'Win32_VideoController; VRAM может быть ограничена 4 ГБ этим API'
    }
}

$selected = $null
foreach ($profile in $profilesDocument.profiles) {
    # Windows reports a nominal 16/32 GB kit slightly below the marketed value.
    $ramEligible = ($ramGb + 0.5) -ge [double]$profile.minimum_ram_gb
    if ($vramMb -ge [int]$profile.minimum_vram_mb -and $ramEligible) {
        $selected = $profile
    }
}
if (-not $selected) { $selected = $profilesDocument.profiles | Select-Object -First 1 }

$projectDrive = [IO.Path]::GetPathRoot($ProjectRoot)
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($projectDrive.TrimEnd('\'))'" -ErrorAction SilentlyContinue
$freeGb = if ($disk) { [Math]::Round([double]$disk.FreeSpace / 1GB, 1) } else { $null }

$report = [ordered]@{
    schema_version = 1
    checked_at = [DateTimeOffset]::Now.ToString('o')
    read_only_probe = $true
    windows = [ordered]@{
        caption = [string]$operatingSystem.Caption
        version = [string]$operatingSystem.Version
        architecture = [string]$operatingSystem.OSArchitecture
    }
    cpu = [ordered]@{
        name = [string]$processor.Name
        logical_processors = [int]$computer.NumberOfLogicalProcessors
    }
    memory = [ordered]@{
        physical_ram_gb = $ramGb
    }
    gpu = [ordered]@{
        name = [string]$gpuName
        vram_mb = $vramMb
        driver = [string]$driverVersion
        source = [string]$gpuSource
        nvidia_smi_available = [bool]$nvidiaSmi
    }
    storage = [ordered]@{
        project_drive = $projectDrive
        free_gb = $freeGb
    }
    recommendation = [ordered]@{
        profile = [string]$selected.id
        label = [string]$selected.label_ru
        context = [int]$selected.recommended_context
        gpu_layers = $selected.gpu_layers
        cache_type_k = [string]$selected.cache_type_k
        cache_type_v = [string]$selected.cache_type_v
        fit_target_mb = [int]$selected.fit_target_mb
        guidance = [string]$selected.guidance_ru
        automatic_model_selection = $false
    }
}

if (-not $OutputPath) { $OutputPath = Join-Path $ProjectRoot 'runtime\hardware\latest.json' }
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
$temporary = "$OutputPath.tmp"
[IO.File]::WriteAllText($temporary, (($report | ConvertTo-Json -Depth 10) + "`n"), $utf8)
Move-Item -Force -LiteralPath $temporary -Destination $OutputPath

Write-Host "Оборудование: $gpuName; VRAM $vramMb МБ; ОЗУ $ramGb ГБ."
Write-Host "Безопасный стартовый профиль: $($selected.label_ru)."
Write-Host "Контекст: $($selected.recommended_context); KV: K=$($selected.cache_type_k), V=$($selected.cache_type_v)."
Write-Host ([string]$selected.guidance_ru)
Write-Host 'Профиль не выбирает и не загружает модель автоматически.'
Write-Host "Отчёт: $OutputPath"
