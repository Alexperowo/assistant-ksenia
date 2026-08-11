param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System
$player = [System.Media.SoundPlayer]::new((Resolve-Path -LiteralPath $Path))
try {
    $player.PlaySync()
}
finally {
    $player.Dispose()
}
