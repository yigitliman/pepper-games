# Ensures the Windows Mobile Hotspot "Pepper" is running (passphrase from the
# PEPPER_SSH_PASSWORD environment variable -- same password Pepper's SSH uses).
# Idempotent: configures + starts it only if it is not already on.
# Internet is shared from whatever connection currently provides it (Ethernet).

if (-not $env:PEPPER_SSH_PASSWORD) {
  Write-Host "PEPPER_SSH_PASSWORD is not set -- cannot configure the hotspot passphrase." -ForegroundColor Red
  exit 1
}

Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction SilentlyContinue

function Await($t, $rt) {
  $m = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  $g = $m.MakeGenericMethod($rt); $nt = $g.Invoke($null, @($t)); $nt.Wait(-1) | Out-Null; $nt.Result
}
function AwaitAction($a) {
  $m = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
  $nt = $m.Invoke($null, @($a)); $nt.Wait(-1) | Out-Null
}

[void][Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType = WindowsRuntime]
[void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType = WindowsRuntime]
[void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration, Windows.Networking.NetworkOperators, ContentType = WindowsRuntime]

$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if ($null -eq $profile) {
  Write-Host "Hotspot: no internet connection (Ethernet) found to share." -ForegroundColor Yellow
  exit 1
}
$mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)

if ($mgr.TetheringOperationalState -eq 'On') {
  Write-Host "Hotspot already on (SSID: Pepper)." -ForegroundColor Green
  exit 0
}

$cfg = New-Object Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration
$cfg.Ssid = "Pepper"
$cfg.Passphrase = $env:PEPPER_SSH_PASSWORD
AwaitAction ($mgr.ConfigureAccessPointAsync($cfg))
$res = Await ($mgr.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
Start-Sleep -Seconds 2
if ($mgr.TetheringOperationalState -eq 'On') {
  Write-Host "Hotspot started (SSID: Pepper). Pepper will connect in a few seconds." -ForegroundColor Green
  exit 0
} else {
  Write-Host ("Could not start hotspot: {0}" -f $res.Status) -ForegroundColor Red
  exit 1
}
