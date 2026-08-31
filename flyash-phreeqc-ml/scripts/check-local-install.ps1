param([switch]$Help)
$ErrorActionPreference = "Stop"
if ($Help) { Write-Host "Usage: .\scripts\check-local-install.ps1"; Write-Host "Validate Compose, health, and the official PHREEQC example."; exit 0 }
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Project
try {
    & docker compose -f docker-compose.local.yml config --quiet
    if ($LASTEXITCODE) { throw "Compose validation failed." }
    $Port = if ($env:WPI_PORT) { $env:WPI_PORT } else { "8501" }
    $Response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 "http://127.0.0.1:$Port/_stcore/health"
    if ($Response.StatusCode -ne 200) { throw "Health endpoint failed." }
    $Command = 'set -eu; phreeqc /opt/phreeqc/share/examples/ex1 /tmp/ex1.pqo "$PHREEQC_DATABASE" >/tmp/ex1.screen 2>&1; test -s /tmp/ex1.pqo; echo "59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10  /opt/phreeqc/database/phreeqc.dat" | sha256sum -c -'
    & docker compose -f docker-compose.local.yml run --rm --no-deps -T --workdir /tmp --entrypoint /bin/sh app -c $Command
    if ($LASTEXITCODE) { throw "PHREEQC runtime preflight failed." }
} finally { Pop-Location }
Write-Host "Local install check passed. The official example verifies runtime operability only."
