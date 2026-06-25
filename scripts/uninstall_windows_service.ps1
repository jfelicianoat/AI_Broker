param(
  [string]$ServiceName = "AI-Broker",
  [string]$Nssm = "nssm.exe"
)

$ErrorActionPreference = "Stop"
$nssmCommand = Get-Command $Nssm -ErrorAction SilentlyContinue
if (-not $nssmCommand) {
  throw "NSSM no encontrado. Pasa -Nssm C:\ruta\nssm.exe"
}

if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
  Stop-Service -Name $ServiceName -ErrorAction SilentlyContinue
}
& $nssmCommand.Source remove $ServiceName confirm
Write-Host "Servicio eliminado: $ServiceName"
