# Lets the laptop or tablet reach host mode through Windows Firewall, or takes that back.
#
#   host-firewall.ps1 -ExePath "C:\...\CaptureKVM.exe"
#   host-firewall.ps1 -ExePath "C:\...\CaptureKVM.exe" -Remove
#
# Run elevated by the installer when "the PC being watched" is chosen, and by the uninstaller. The
# ports are Beamer's: 24820 for the link, 24821 for pairing over UDP and TCP. Private networks
# only, as Beamer's own installer does it: a home network, not a cafe's.
#
# The rules have CaptureKVM's own names, never Beamer's. Beamer's uninstaller deletes its rules by
# name, so a CaptureKVM rule called "Beamer Receiver" would vanish when an old Beamer on the same
# PC is uninstalled -- and removing ours by that name would take the old Beamer's with it.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$Rules = @(
    @{ Name = 'CaptureKVM Host (TCP-In)';    Protocol = 'TCP'; Port = 24820 },
    @{ Name = 'CaptureKVM Pairing (UDP-In)'; Protocol = 'UDP'; Port = 24821 },
    @{ Name = 'CaptureKVM Pairing (TCP-In)'; Protocol = 'TCP'; Port = 24821 }
)

try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Changing the firewall needs administrator permission.'
    }
    if ([IO.Path]::GetFileName($ExePath) -ine 'CaptureKVM.exe' -or -not [IO.Path]::IsPathRooted($ExePath)) {
        throw 'The path must be the full path of the installed CaptureKVM.exe.'
    }
    $ExePath = [IO.Path]::GetFullPath($ExePath)

    # Remove first either way, so installing twice leaves one set of rules, not two.
    foreach ($rule in $Rules) {
        Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    }
    if (-not $Remove) {
        if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) { throw "There is no CaptureKVM.exe at $ExePath." }
        foreach ($rule in $Rules) {
            New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Protocol $rule.Protocol `
                -LocalPort $rule.Port -Action Allow -Profile Private -Program $ExePath | Out-Null
        }
    }
    exit 0
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
