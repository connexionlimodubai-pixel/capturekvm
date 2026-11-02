# Does the built exe actually start, in both modes?
#
#   .\smoke.ps1
#   .\smoke.ps1 -Dist path\to\CaptureKVM
#
# build.ps1's import probe proves every module is in the bundle. It does not prove a window
# appears: a missing Qt plugin, a broken data file or a mode that hands the process over badly
# all import cleanly and then show nothing at all -- and a windowed exe that dies on startup
# prints nothing anywhere. So each mode is started for real and identified by its window title.
#
# Needs a desktop session. Nothing is paired, no card is opened, no link is made, and the config
# file is not touched: both modes are started with an explicit --mode, which never reads or
# writes the remembered one, and the picker is started with --pick and closed unanswered.

[CmdletBinding()]
param([string]$Dist = (Join-Path $PSScriptRoot "dist\CaptureKVM"), [int]$TimeoutSeconds = 25)

$ErrorActionPreference = "Stop"
$exe = Join-Path $Dist "CaptureKVM.exe"
if (-not (Test-Path $exe)) { throw "no exe at $exe -- run .\build.ps1 first" }

$passed = 0
$failed = 0

function Check([string]$name, [bool]$ok, [string]$detail = "") {
    if ($ok) { Write-Host "  ok   $name" -ForegroundColor Green; $script:passed++ }
    else { Write-Host "  FAIL $name -- $detail" -ForegroundColor Red; $script:failed++ }
}

function Start-Mode([string[]]$Arguments, [string]$Expect, [string]$What) {
    Write-Host "`n$What ($($Arguments -join ' '))"
    $process = Start-Process -FilePath $exe -ArgumentList $Arguments -PassThru
    try {
        $title = ""
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 400
            # The exe may hand off to a window owned by a different process id (an elevated
            # relaunch), so every CaptureKVM process is considered, not just the one started.
            $titles = @(Get-Process -Name CaptureKVM -ErrorAction SilentlyContinue |
                        Where-Object { $_.MainWindowTitle } | ForEach-Object { $_.MainWindowTitle })
            # @() around the filter as well as the source: a pipeline that matches one thing
            # returns that string, not a one-item array, and indexing a string gives a letter.
            $found = @($titles | Where-Object { $_ -like $Expect })
            if ($found.Count) { $title = $found[0]; break }
            if ($titles.Count) { $title = $titles[0] }
            if ($process.HasExited) { break }
        }
        Check "a window titled '$Expect' came up" ($title -like $Expect) `
              "saw '$title'; exited=$($process.HasExited)"
        return $title
    }
    finally {
        Get-Process -Name CaptureKVM -ErrorAction SilentlyContinue |
            ForEach-Object { try { $_.CloseMainWindow() | Out-Null } catch {} }
        Start-Sleep -Milliseconds 800
        Get-Process -Name CaptureKVM -ErrorAction SilentlyContinue |
            ForEach-Object { try { Stop-Process -Id $_.Id -Force } catch {} }
        Start-Sleep -Milliseconds 400
    }
}

Write-Host "smoke: the built exe in each mode" -ForegroundColor Cyan

Start-Mode @("--pick") "CaptureKVM * choose a mode" "1. The picker" | Out-Null
Start-Mode @("--mode", "receiver") "CaptureKVM 0.*" "2. Receiver mode: this app's own window" | Out-Null
$hostTitle = Start-Mode @("--mode", "host") "Beamer*" "3. Host mode: the Beamer fork's window"

$log = Join-Path $env:LOCALAPPDATA "Beamer\Beamer.log"
if (Test-Path $log) {
    $recent = Get-Content -LiteralPath $log -Tail 40
    Check "host mode logged its start" ([bool]($recent | Where-Object { $_ -like "*Starting Beamer Windows receiver*" })) `
          "no start line in the last 40 of $log"
    $pad = $recent | Where-Object { $_ -like "*pad*could not be plugged in*" }
    if ($pad) { Write-Host "  note the virtual pad was refused: $($pad[-1])" -ForegroundColor Yellow }
} else {
    Write-Host "  note no $log yet" -ForegroundColor DarkGray
}

Write-Host "`n$passed passed, $failed failed" -ForegroundColor $(if ($failed) { "Red" } else { "Green" })
if ($failed) { exit 1 }
