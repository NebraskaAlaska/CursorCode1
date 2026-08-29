param([string]$Archive, [switch]$Yes, [switch]$Help)
$ErrorActionPreference = "Stop"
if ($Help) { Write-Host "Usage: .\scripts\restore-local.ps1 -Archive FILE -Yes"; Write-Host "Set WPI_COMPOSE_FILE for a non-local stack."; exit 0 }
if ([string]::IsNullOrWhiteSpace($Archive)) { throw "Restore archive is required; pass -Archive FILE." }
if (-not $Yes) { throw "Restore can overwrite durable paths; pass -Yes to confirm." }
$Archive = (Resolve-Path $Archive).Path
$ChecksumPath = "$Archive.sha256"
if (-not (Test-Path -PathType Leaf $ChecksumPath)) { throw "Backup checksum sidecar is required: $ChecksumPath" }
$Expected = ((Get-Content -TotalCount 1 $ChecksumPath) -split '\s+')[0].ToLowerInvariant()
if ($Expected -notmatch '^[0-9a-f]{64}$') { throw "Backup checksum sidecar is malformed." }
$Actual = (Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant()
if ($Expected -ne $Actual) { throw "Backup checksum mismatch." }
$Members = @(& tar -tzf $Archive)
if ($LASTEXITCODE) { throw "Backup archive cannot be read safely." }
$Listing = @(& tar -tvzf $Archive)
if ($LASTEXITCODE) { throw "Backup archive cannot be read safely." }
foreach ($Line in $Listing) {
    if ($Line -and $Line[0] -notin @('-', 'd')) { throw "Archive contains a link or special file." }
}
foreach ($Member in $Members) {
    if ($Member -match '(^/|(^|/)\.\.(/|$)|\\)') { throw "Unsafe archive member." }
    if ($Member -notmatch '^(app/(outputs|experiments|data/processed|imports)|var/lib/wpi/resources)(/|$)') {
        throw "Unexpected archive member: $Member"
    }
}
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeFile = if ([string]::IsNullOrWhiteSpace($env:WPI_COMPOSE_FILE)) { "docker-compose.local.yml" } else { $env:WPI_COMPOSE_FILE }
Push-Location $Project
try {
    if (-not (Test-Path -PathType Leaf $ComposeFile)) { throw "Compose file not found: $ComposeFile" }
    & docker compose -f $ComposeFile stop app
    if ($LASTEXITCODE) { throw "Could not stop the app before restore." }
    & docker compose -f $ComposeFile run --rm --no-deps -T --user 0:0 --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --entrypoint sh app -c 'find /app/outputs /app/experiments /app/data/processed /app/imports /var/lib/wpi/resources -xdev -mindepth 1 -depth -delete'
    if ($LASTEXITCODE) { throw "Could not clear the five exact durable roots before restore." }
    $RestoreCommand = 'set -eu; tar --no-same-owner --no-same-permissions -xzf /restore/backup.tar.gz -C /; chown -R 10001:10001 /app/outputs /app/experiments /app/data/processed /app/imports /var/lib/wpi/resources; chmod 0750 /app/outputs /app/experiments /app/data/processed /app/imports /var/lib/wpi/resources'
    & docker compose -f $ComposeFile run --rm --no-deps -T --user 0:0 --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER -v "${Archive}:/restore/backup.tar.gz:ro" --entrypoint sh app -c $RestoreCommand
    if ($LASTEXITCODE) { throw "Restore failed." }
    & docker compose -f $ComposeFile up --detach app
    if ($LASTEXITCODE) { throw "Restore completed but the app could not be restarted." }
} finally { Pop-Location }
Write-Host "Restore completed. Review the restored workspace before research use."
