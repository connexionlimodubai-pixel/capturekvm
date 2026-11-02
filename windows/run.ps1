# Starts the Windows client, creating the virtual environment on first run.
#
#   .\run.ps1
#
# Uses .venv beside this script. Delete that folder to start over.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating .venv ..." -ForegroundColor Cyan
    python -m venv .venv
    & $python -m pip install --upgrade pip
    Write-Host "Installing dependencies ..." -ForegroundColor Cyan
    & $python -m pip install -r requirements.txt
}

& $python -m capturekvm @args
