#!/usr/bin/env sh
set -eu

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
    echo "Usage: scripts/run_nsf_demo.sh [--build]"
    echo "Launch and preflight the clearly labeled SYNTHETIC DEMO route."
    exit 0
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$script_dir/launch-local.sh" "${1:-}"

attempt=0
until "$script_dir/check-local-install.sh"; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 12 ] || { echo "Demo preflight did not become healthy." >&2; exit 1; }
    sleep 5
done

port=${WPI_PORT:-8501}
echo ""
echo "SYNTHETIC DEMO — no output in this route is experimental validation."
echo "Open http://127.0.0.1:$port"
echo "Follow docs/NSF_DEMO_SCRIPT.md and keep provenance/status labels visible."
