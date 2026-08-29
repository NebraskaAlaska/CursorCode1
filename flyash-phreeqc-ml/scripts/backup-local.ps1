param([string]$Destination, [switch]$Help)
$ErrorActionPreference = "Stop"
if ($Help) { Write-Host "Usage: .\scripts\backup-local.ps1 [-Destination PATH]"; Write-Host "Creates an unencrypted durable-data archive. Set WPI_COMPOSE_FILE for a non-local stack."; exit 0 }
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeFile = if ([string]::IsNullOrWhiteSpace($env:WPI_COMPOSE_FILE)) { "docker-compose.local.yml" } else { $env:WPI_COMPOSE_FILE }
if (-not $Destination) { $Destination = Join-Path $Project "backups" }
New-Item -ItemType Directory -Force $Destination | Out-Null
$Destination = (Resolve-Path $Destination).Path
$Name = "wpi-virtual-lab-{0}-{1}.tar.gz" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"), $PID
$PartialName = ".$Name.partial.$PID"
$PartialPath = Join-Path $Destination $PartialName
$FinalPath = Join-Path $Destination $Name
if (Test-Path $FinalPath) { throw "Backup target already exists: $FinalPath" }
$AppStopped = $false
Push-Location $Project
try {
    if (-not (Test-Path -PathType Leaf $ComposeFile)) { throw "Compose file not found: $ComposeFile" }
    & docker compose -f $ComposeFile stop app
    if ($LASTEXITCODE) { throw "Could not stop the app before backup." }
    $AppStopped = $true
    & docker compose -f $ComposeFile run --rm --no-deps -T -v "${Destination}:/backup" --entrypoint tar app -czf "/backup/$PartialName" -C / app/outputs app/experiments app/data/processed app/imports var/lib/wpi/resources
    if ($LASTEXITCODE) { throw "Backup failed." }
    & docker compose -f $ComposeFile up --detach app
    if ($LASTEXITCODE) { throw "Backup completed but the app could not be restarted." }
    $AppStopped = $false
    Move-Item $PartialPath $FinalPath
} finally {
    if ($AppStopped) {
        & docker compose -f $ComposeFile up --detach app | Out-Null
        if ($LASTEXITCODE) { Write-Warning "Backup failed and the app could not be restarted; operator action is required." }
    }
    Pop-Location
    if (Test-Path $PartialPath) { Remove-Item -Force $PartialPath }
}
$Hash = (Get-FileHash -Algorithm SHA256 $FinalPath).Hash.ToLowerInvariant()
Set-Content -NoNewline -Encoding ascii (Join-Path $Destination "$Name.sha256") "$Hash  $Name`n"
Write-Host "Backup: $FinalPath"
Write-Host "This archive can contain private research records and is not encrypted."
