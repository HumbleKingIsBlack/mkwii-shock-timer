$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Shock Timer - Windows setup" -ForegroundColor Cyan

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python was not found. Install Python 3.11 or newer from python.org and enable 'Add Python to PATH'."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing FFmpeg with winget..."
        winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    } else {
        throw "FFmpeg was not found. Install it from ffmpeg.org and add its bin folder to PATH."
    }
}

Write-Host "Creating the local Python environment..."
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt

$ConfigDirectory = Join-Path $env:APPDATA "ShockTimer"
$ConfigFile = Join-Path $ConfigDirectory "config.json"
New-Item -ItemType Directory -Force -Path $ConfigDirectory | Out-Null
if (-not (Test-Path $ConfigFile)) {
    @{
        audio_device = ""
        alert_sound = ""
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $ConfigFile
}

Write-Host ""
Write-Host "Setup complete. Double-click launch-windows.bat to start Shock Timer." -ForegroundColor Green
