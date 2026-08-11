param(
    [string]$Text = '',
    [int]$Rate = 0
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
if ([string]::IsNullOrEmpty($Text)) {
    $Text = [Console]::In.ReadToEnd()
}
if ([string]::IsNullOrWhiteSpace($Text)) { exit 0 }
Add-Type -AssemblyName System.Speech
$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $speaker.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))
    $speaker.Speak($Text)
}
finally {
    $speaker.Dispose()
}
