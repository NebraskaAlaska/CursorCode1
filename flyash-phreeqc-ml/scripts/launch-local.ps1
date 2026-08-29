param([switch]$Build, [switch]$Help)
$ErrorActionPreference = "Stop"
if ($Help) {
    Write-Host "Usage: .\scripts\launch-local.ps1 [-Build]"
    Write-Host "Launch the loopback-only local stack; AI is disabled unless configured."
    exit 0
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker is required." }
& docker compose version | Out-Null
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Project
try {
    $Args = @("compose", "-f", "docker-compose.local.yml", "up", "--detach")
    if ($Build) { $Args += "--build" }
    & docker @Args
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose launch failed." }
} finally { Pop-Location }
$Port = if ($env:WPI_PORT) { $env:WPI_PORT } else { "8501" }
Write-Host "WPI Virtual LAB is starting at http://127.0.0.1:$Port"
