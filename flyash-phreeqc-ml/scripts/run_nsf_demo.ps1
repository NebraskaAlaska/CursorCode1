param([switch]$Build, [switch]$Help)
$ErrorActionPreference = "Stop"
if ($Help) { Write-Host "Usage: .\scripts\run_nsf_demo.ps1 [-Build]"; Write-Host "Launch the SYNTHETIC DEMO route."; exit 0 }
& (Join-Path $PSScriptRoot "launch-local.ps1") -Build:$Build
for ($Attempt = 0; $Attempt -lt 12; $Attempt++) {
    try { & (Join-Path $PSScriptRoot "check-local-install.ps1"); break }
    catch { if ($Attempt -eq 11) { throw }; Start-Sleep -Seconds 5 }
}
$Port = if ($env:WPI_PORT) { $env:WPI_PORT } else { "8501" }
Write-Host "SYNTHETIC DEMO - no output in this route is experimental validation."
Write-Host "Open http://127.0.0.1:$Port and follow docs/NSF_DEMO_SCRIPT.md."
