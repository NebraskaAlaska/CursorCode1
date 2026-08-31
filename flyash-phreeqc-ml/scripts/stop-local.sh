#!/usr/bin/env sh
set -eu

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
    echo "Usage: scripts/stop-local.sh"
    echo "Stop local containers without removing durable named volumes."
    exit 0
fi
[ "$#" -eq 0 ] || { echo "Usage: scripts/stop-local.sh" >&2; exit 64; }
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
docker compose -f docker-compose.local.yml down
echo "Stopped. Durable WPI volumes were retained."
