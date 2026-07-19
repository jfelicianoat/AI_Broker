param(
  [string]$ServiceName = "AI-Broker",
  [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
  [string]$Python = "python",
  [string]$Config = "broker_config.yaml",
  [string]$Nssm = "nssm.exe"
)

$ErrorActionPreference = "Stop"
$nssmCommand = Get-Command $Nssm -ErrorAction SilentlyContinue
if (-not $nssmCommand) {
  throw "NSSM no encontrado. Instala NSSM y vuelve a ejecutar, o pasa -Nssm C:\ruta\nssm.exe"
}

$script = Join-Path $ProjectRoot "scripts\run_broker.py"
$logs = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

& $nssmCommand.Source install $ServiceName $Python "`"$script`" --config `"$Config`""
& $nssmCommand.Source set $ServiceName AppDirectory $ProjectRoot
& $nssmCommand.Source set $ServiceName AppStdout (Join-Path $logs "service.stdout.log")
& $nssmCommand.Source set $ServiceName AppStderr (Join-Path $logs "service.stderr.log")
& $nssmCommand.Source set $ServiceName AppRotateFiles 1
& $nssmCommand.Source set $ServiceName AppRotateBytes 10485760
& $nssmCommand.Source set $ServiceName Start SERVICE_AUTO_START
& $nssmCommand.Source set $ServiceName AppRestartDelay 5000

Write-Host "Servicio instalado: $ServiceName"
Write-Host "Arranque: Start-Service '$ServiceName'"
Write-Host "Readiness: python scripts/check_readiness.py --url http://127.0.0.1:8765/health/ready"
