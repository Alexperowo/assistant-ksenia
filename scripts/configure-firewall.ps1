# Ksenia Local LAN PWA Firewall Configuration
$ErrorActionPreference = "Stop"

$ruleName = "Ksenia-LAN-PWA"
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Remove-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
}

New-NetFirewallRule -Name $ruleName `
    -DisplayName "Ксения — Локальная панель PWA" `
    -Description "Входящий доступ HTTPS для панели Ксении в домашней сети" `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 8765 `
    -Profile Private `
    -RemoteAddress LocalSubnet `
    -Enabled True | Out-Null

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  [OK] Порт 8765 для Ксении успешно открыт в брандмауэре!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Профиль: Домашняя частная сеть (Private)" -ForegroundColor Gray
Write-Host "  Диапазон: Только устройства локальной сети (LocalSubnet)" -ForegroundColor Gray
Write-Host "  Порт: 8765 TCP (HTTPS)" -ForegroundColor Gray
Write-Host ""
