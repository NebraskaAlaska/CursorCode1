#!/usr/bin/env sh
set -eu

usage() {
    echo "Usage: scripts/launch-local.sh [--build]"
    echo "Launch the loopback-only local WPI Virtual LAB stack. AI is disabled unless configured."
}

build=false
case "${1:-}" in
    "") ;;
    --build) build=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
esac

command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 69; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required." >&2; exit 69; }
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

if [ "$build" = true ]; then
    docker compose -f docker-compose.local.yml up --detach --build
else
    docker compose -f docker-compose.local.yml up --detach
fi

port=${WPI_PORT:-8501}
echo "WPI Virtual LAB is starting at http://127.0.0.1:$port"
echo "Run scripts/check-local-install.sh before a demonstration."
