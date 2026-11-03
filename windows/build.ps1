# Builds the one exe that is both ends of the link, then checks it can import both.
#
#   .\build.ps1
#   .\build.ps1 -Zip            also writes dist\CaptureKVM-windows-x64.zip
#
# Uses .venv beside this script, creating it on first run. The import probe at the end is the
# point of having a script rather than a bare pyinstaller call: a windowed exe cannot tell you it
# is missing a module -- it just fails to start with no window and no message -- so every module
# of both modes is loaded back out of the finished build and the outcome is read from a file.

[CmdletBinding()]
param(
    [switch]$Zip,
    # Also compile installer\CaptureKVM.iss into dist\CaptureKVM-Setup-<version>.exe. Needs Inno
    # Setup 6: its ISCC.exe is found through -Iscc, the ISCC environment variable, PATH, or the
    # places Inno Setup installs itself.
    [switch]$Installer,
    [string]$Iscc,
    # CI installs the requirements into the runner's own Python and has no .venv; `-Python python`
    # builds with that instead of making a second environment to install them into again.
    [string]$Python
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# PyInstaller writes its progress to stderr. Windows PowerShell turns a native command's stderr
# into error records, and with ErrorActionPreference Stop a perfectly good build then "fails" on
# its first INFO line. Exit codes are checked by hand below instead.
$PSNativeCommandUseErrorActionPreference = $false
function Invoke-Native {
    param([string]$Exe, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Exe @Arguments } finally { $ErrorActionPreference = $previous }
}

if ($Python) {
    $python = $Python
} else {
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        Write-Host "Creating .venv ..." -ForegroundColor Cyan
        Invoke-Native python -m venv .venv
        Invoke-Native $python -m pip install --upgrade pip
        Invoke-Native $python -m pip install -r requirements.txt
    }
}

# ViGEmBus's client library, which host mode needs to plug a controller in. Fetched, not committed;
# see tools/fetch_vigem_client.py for why it has to be here and where it comes from.
Write-Host "Fetching ViGEmClient.dll ..." -ForegroundColor Cyan
Invoke-Native $python ..\tools\fetch_vigem_client.py
if ($LASTEXITCODE -ne 0) { throw "could not fetch ViGEmClient.dll" }

Write-Host "Building ..." -ForegroundColor Cyan
Invoke-Native $python -m PyInstaller capturekvm.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$dist = Join-Path $PSScriptRoot "dist\CaptureKVM"
$exe = Join-Path $dist "CaptureKVM.exe"
if (-not (Test-Path $exe)) { throw "no exe at $exe" }

# Host mode is Beamer's own window and has no button of ours in it, so a machine that answered
# "host" and meant "receiver" needs a way back. Double-clicking this is it.
@'
@echo off
rem Asks again which end of the link this machine is, and starts that mode.
start "" "%~dp0CaptureKVM.exe" --pick
'@ | Set-Content -LiteralPath (Join-Path $dist "Change mode.cmd") -Encoding ascii

Write-Host "Probing both modes' imports inside the build ..." -ForegroundColor Cyan
# Every module name the spec bundles, asked of the exe itself. --import-probe is Beamer's own
# switch; capturekvm.launcher routes it to host mode, which is the half with more to go wrong.
$modules = Invoke-Native $python -c @"
import pathlib, sys
root = pathlib.Path('..').resolve()
win_app, core = root / 'beamer-vigem' / 'win_app', root / 'beamer-vigem' / 'core'
names = ['kvm_bridge_win', 'capturekvm.launcher', 'capturekvm.app', 'capturekvm.link',
         'capturekvm.capture', 'capturekvm.gamepad', 'capturekvm.audio', 'cv2', 'numpy',
         'sounddevice', 'nacl', 'segno', 'win32com.client']
names += sorted(p.stem for p in win_app.glob('*.py') if p.stem != 'kvm_bridge_win')
names += sorted(f'core.{p.stem}' for p in core.glob('*.py') if p.stem != '__init__')
print('\n'.join(names))
"@
if ($LASTEXITCODE -ne 0) { throw "could not list the modules to probe" }

$names = @($modules -split "`r?`n" | Where-Object { $_ })
if ($names.Count -lt 50) { throw "only $($names.Count) module names to probe; that cannot be right" }

$report = Join-Path ([System.IO.Path]::GetTempPath()) "capturekvm-import-probe.txt"
if (Test-Path $report) { Remove-Item -LiteralPath $report }
# Start-Process -Wait, not the call operator: the exe is windowed, so it never attaches to this
# console and `& $exe` returns the moment it is launched. The first version of this read the
# report before the exe had written a line of it and called a 0-module probe a pass.
$probe = Start-Process -FilePath $exe -ArgumentList (@("--import-probe", $report) + $names) `
                       -PassThru -Wait
$lines = if (Test-Path $report) { @(Get-Content -LiteralPath $report) } else { @() }
if ($lines.Count -eq 0) { throw "the probe wrote nothing; the exe did not start" }

$failed = $lines | Where-Object { $_ -like "FAIL *" }
if ($failed) {
    ($lines | Select-Object -Last 25) | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw "the build is missing modules it needs"
}
if ($probe.ExitCode -ne 0) { throw "the import probe failed with exit $($probe.ExitCode)" }
# The probe stops at the first failure, so a short report is a failure too.
if ($lines.Count -ne $names.Count) {
    throw "the probe reported $($lines.Count) of $($names.Count) modules"
}
Write-Host "  ok  $($lines.Count) modules imported from the exe" -ForegroundColor Green

# The import probe cannot see this one: host mode loads it through ctypes when a controller is
# first plugged in. Its absence was invisible until a user's controller did nothing. So it is
# checked by hand, in the folder host mode looks in, and against the hash it was fetched by.
$client = Join-Path $dist "_internal\ViGEmClient.dll"
if (-not (Test-Path $client)) { throw "ViGEmClient.dll is not in the build; host mode has no controller" }
$wanted = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot "vendor\ViGEmClient.dll")).Hash
$shipped = (Get-FileHash -Algorithm SHA256 -LiteralPath $client).Hash
if ($shipped -ne $wanted) { throw "the bundled ViGEmClient.dll is not the one fetched" }
Write-Host "  ok  ViGEmClient.dll is in the build, where host mode looks for it" -ForegroundColor Green

if ($Zip) {
    $archive = Join-Path $PSScriptRoot "dist\CaptureKVM-windows-x64.zip"
    if (Test-Path $archive) { Remove-Item -LiteralPath $archive }
    Write-Host "Zipping ..." -ForegroundColor Cyan
    Compress-Archive -Path $dist -DestinationPath $archive
    Write-Host ("  ok  {0} ({1:N1} MB)" -f $archive, ((Get-Item $archive).Length / 1MB)) -ForegroundColor Green
}

if ($Installer) {
    $candidates = @($Iscc, $env:ISCC, (Get-Command iscc -ErrorAction SilentlyContinue).Source,
                    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
                    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
                    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"))
    $compiler = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $compiler) { throw "Inno Setup 6 was not found: pass -Iscc C:\path\to\ISCC.exe or set ISCC" }

    # The Windows version lives in one place, capturekvm/__init__.py; the installer takes it from there.
    $match = Select-String -LiteralPath (Join-Path $PSScriptRoot "capturekvm\__init__.py") -Pattern '^__version__ = "([^"]+)"'
    if (-not $match) { throw "no __version__ in capturekvm/__init__.py" }
    $version = $match.Matches[0].Groups[1].Value

    Write-Host "Compiling the installer for $version ..." -ForegroundColor Cyan
    Invoke-Native $compiler /Qp "/DAppVersion=$version" (Join-Path $PSScriptRoot "installer\CaptureKVM.iss")
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
    $setup = Join-Path $PSScriptRoot "dist\CaptureKVM-Setup-$version.exe"
    if (-not (Test-Path -LiteralPath $setup)) { throw "ISCC finished but wrote no $setup" }
    Write-Host ("  ok  {0} ({1:N1} MB)" -f $setup, ((Get-Item $setup).Length / 1MB)) -ForegroundColor Green
}

$size = (Get-ChildItem -LiteralPath $dist -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("Done. {0} ({1:N0} MB)" -f $dist, ($size / 1MB)) -ForegroundColor Green
