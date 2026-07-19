[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$RuleName = "AI Broker LAN",
  [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
if ($PSCmdlet.ShouldProcess("Windows Firewall", "Allow inbound TCP $Port from LocalSubnet")) {
  New-NetFirewallRule `
    -DisplayName $RuleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $Port `
    -RemoteAddress LocalSubnet `
    -Profile Private `
    -Enabled True
}
