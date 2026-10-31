# Starts AdRx and opens it in the browser. Easiest: double-click start.bat (optionally: start.bat 8001).
param([int]$Port = 8000)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$onWindows = ($env:OS -eq 'Windows_NT')
if ($onWindows) { $venvPython = Join-Path $root '.venv\Scripts\python.exe' } else { $venvPython = Join-Path $root '.venv/bin/python' }

if (-not (Test-Path $venvPython)) {
    Write-Host 'Run setup.bat first: it installs everything the app needs.' -ForegroundColor Red
    exit 1
}

$busy = $false
try {
    $probe = New-Object System.Net.Sockets.TcpClient
    $probe.Connect('127.0.0.1', $Port)
    $busy = $true
    $probe.Close()
} catch { }
if ($busy) {
    Write-Host "Port $Port is already in use (is AdRx already running?). Close it, or start on another port: start.bat 8001" -ForegroundColor Yellow
    exit 1
}

$url = "http://localhost:$Port"
Write-Host "Starting AdRx at $url" -ForegroundColor Cyan
Write-Host 'Keep this window open while you use the app. Press Ctrl+C here to stop it.'

if ($onWindows) {
    # Open the browser as soon as the app answers (runs in a hidden helper window).
    $opener = "for (`$i = 0; `$i -lt 120; `$i++) { try { Invoke-WebRequest -UseBasicParsing -Uri '$url/api/health' -TimeoutSec 2 | Out-Null; Start-Process '$url'; break } catch { Start-Sleep -Milliseconds 500 } }"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($opener))
    Start-Process powershell -WindowStyle Hidden -ArgumentList @('-NoProfile', '-EncodedCommand', $encoded)
}

& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
