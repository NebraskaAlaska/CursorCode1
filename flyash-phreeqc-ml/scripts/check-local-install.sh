#!/usr/bin/env sh
set -eu

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
    echo "Usage: scripts/check-local-install.sh"
    echo "Validate Compose, app health, and the bundled official PHREEQC example."
    exit 0
fi
[ "$#" -eq 0 ] || { echo "Usage: scripts/check-local-install.sh" >&2; exit 64; }
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
docker compose -f docker-compose.local.yml config --quiet
docker compose -f docker-compose.local.yml ps

port=${WPI_PORT:-8501}
python3 - "$port" <<'PY'
import sys
import urllib.request

port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=10) as response:
    if response.status != 200:
        raise SystemExit(f"health endpoint returned {response.status}")
PY

docker compose -f docker-compose.local.yml run --rm --no-deps -T \
    --workdir /tmp \
    --entrypoint /bin/sh app -c \
    'set -eu; phreeqc /opt/phreeqc/share/examples/ex1 /tmp/ex1.pqo "$PHREEQC_DATABASE" >/tmp/ex1.screen 2>&1; test -s /tmp/ex1.pqo; sha256sum -c <<EOF
59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10  /opt/phreeqc/database/phreeqc.dat
EOF'
echo "Local install check passed. The official example verifies runtime operability only."
