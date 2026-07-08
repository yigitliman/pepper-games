# Keeps the "Pepper" hotspot on: re-checks every 5 minutes and re-enables it
# if Windows turned it off (e.g. power-saving when no device is connected).
# Started at logon from the Startup folder; no admin rights required.

$ensure = Join-Path $PSScriptRoot 'start_pepper_hotspot.ps1'
while ($true) {
    try { & powershell -NoProfile -ExecutionPolicy Bypass -File $ensure | Out-Null } catch {}
    Start-Sleep -Seconds 300
}
