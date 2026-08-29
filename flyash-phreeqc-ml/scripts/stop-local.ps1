param([switch]$Help)
$ErrorActionPreference = "Stop"
if ($Help) { Write-Host "Usage: .\scripts\stop-local.ps1"; Write-Host "Stops containers and retains named volumes."; exit 0 }
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Project
try { & docker compose -f docker-compose.local.yml down; if ($LASTEXITCODE) { throw "Stop failed." } }
finally { Pop-Location }
Write-Host "Stopped. Durable WPI volumes were retained."
