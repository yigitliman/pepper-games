# start_pepper_admin.ps1 -- launch the Pepper web admin panel.
#
# Usage:
#   $env:HCTL_BASE_URL = "https://your-open-webui-server"
#   $env:HCTL_TOKEN = "eyJhbGci..."      # needed only for the Q&A game
#   .\start_pepper_admin.ps1             # then open http://127.0.0.1:8080
#
# It activates the 'pepper' conda env's Python, puts pynaoqi on PYTHONPATH,
# starts the server and opens your browser.

param(
    [int]$Port = 8080,
    [string]$Ip = "192.168.137.214"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. locate the pepper env python (adjust if your miniconda lives elsewhere)
$python = "$env:USERPROFILE\AppData\Local\miniconda3\envs\pepper\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) { throw "Could not find the 'pepper' env Python. Edit the path in this script." }

# 2. locate pynaoqi (the folder that contains the 'qi' package)
$lib = Get-ChildItem -Path "$root\sdk" -Recurse -Directory -Filter "lib" -ErrorAction SilentlyContinue |
       Where-Object { Test-Path (Join-Path $_.FullName "qi\__init__.py") } |
       Select-Object -First 1 -ExpandProperty FullName
if (-not $lib) { throw "Could not find pynaoqi under $root\sdk. Is the SDK unpacked?" }

$env:PYTHONPATH = $lib
$env:PATH = "$lib;$env:PATH"

if (-not $env:HCTL_BASE_URL) {
    Write-Host "NOTE: HCTL_BASE_URL is not set - the games will not reach the AI server." -ForegroundColor Yellow
}
if (-not $env:HCTL_TOKEN) {
    Write-Host "NOTE: HCTL_TOKEN is not set - the Q&A game will not reach the AI server." -ForegroundColor Yellow
}

$args = @("$root\pepper_admin.py", "--port", "$Port")
if ($Ip) { $args += @("--ip", $Ip) }

Start-Process "http://127.0.0.1:$Port"
& $python @args
