#!/usr/bin/env sh
set -eu
umask 077

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
    echo "Usage: scripts/backup-local.sh [destination-directory]"
    echo "Create an unencrypted archive of the five durable volume roots."
    echo "Set WPI_COMPOSE_FILE=docker-compose.server.yml for the hosted stack."
    exit 0
fi
[ "$#" -le 1 ] || { echo "Usage: scripts/backup-local.sh [destination-directory]" >&2; exit 64; }
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file=${WPI_COMPOSE_FILE:-docker-compose.local.yml}
destination=${1:-"$project_dir/backups"}
mkdir -p "$destination"
destination=$(CDPATH= cd -- "$destination" && pwd)
archive="wpi-virtual-lab-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
partial="$destination/.$archive.partial.$$"
cd "$project_dir"

[ -f "$compose_file" ] || {
    echo "Compose file not found: $compose_file" >&2
    exit 66
}

app_stopped=false
cleanup() {
    status=$?
    trap - 0 1 2 15
    rm -f "$partial"
    if [ "$app_stopped" = true ]; then
        docker compose -f "$compose_file" up --detach app >/dev/null 2>&1 || {
            echo "Backup failed and the app could not be restarted; operator action is required." >&2
        }
    fi
    exit "$status"
}
trap cleanup 0 1 2 15

docker compose -f "$compose_file" stop app
app_stopped=true

docker compose -f "$compose_file" run --rm --no-deps -T \
    --entrypoint tar app -czf - -C / \
    app/outputs app/experiments app/data/processed \
    app/imports var/lib/wpi/resources >"$partial"
docker compose -f "$compose_file" up --detach app
app_stopped=false
mv "$partial" "$destination/$archive"
trap - 0 1 2 15
sha256sum "$destination/$archive" >"$destination/$archive.sha256"
echo "Backup: $destination/$archive"
echo "This archive can contain private research records and is not encrypted. Store it accordingly."
