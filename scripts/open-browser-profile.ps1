$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$defaultConfig = Get-Content -Raw -LiteralPath (Join-Path $projectRoot 'config\default.json') | ConvertFrom-Json
$userConfigPath = Join-Path $projectRoot 'config\user.json'
$userConfig = if (Test-Path -LiteralPath $userConfigPath) {
    Get-Content -Raw -LiteralPath $userConfigPath | ConvertFrom-Json
} else { $null }

$browserExecutable = if ($userConfig.browser.executable) {
    [string]$userConfig.browser.executable
} else { [string]$defaultConfig.browser.executable }
$profileDirectory = if ($userConfig.browser.profile_dir) {
    [string]$userConfig.browser.profile_dir
} else { [string]$defaultConfig.browser.profile_dir }

if (-not (Test-Path -LiteralPath $browserExecutable)) {
    throw "Chromium Ксении не найден: $browserExecutable"
}
New-Item -ItemType Directory -Force -Path $profileDirectory | Out-Null
Start-Process -FilePath $browserExecutable -ArgumentList @(
    "--user-data-dir=$profileDirectory",
    '--no-first-run',
    '--no-default-browser-check',
    'https://www.google.com/'
)
Write-Host 'Открыт отдельный браузер Ксении. Войдите в нужные сайты и затем закройте все его окна.'
