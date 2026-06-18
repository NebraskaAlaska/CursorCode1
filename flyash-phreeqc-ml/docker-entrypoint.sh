#!/usr/bin/env sh
# Container entrypoint: print a DEBUG-SAFE startup status (booleans + non-secret paths only —
# the API key value is NEVER logged), then start Streamlit on $PORT (defaults to 8501).
set -eu

phreeqc_ok=false
if [ -x "${PHREEQC_EXE:-}" ] || command -v "${PHREEQC_EXE:-phreeqc}" >/dev/null 2>&1; then
    phreeqc_ok=true
fi
db_ok=false
[ -n "${PHREEQC_DATABASE:-}" ] && [ -f "${PHREEQC_DATABASE}" ] && db_ok=true
key_present=false
[ -n "${ANTHROPIC_API_KEY:-}" ] && key_present=true

echo "[startup] phreeqc_exe=${PHREEQC_EXE:-phreeqc} found=${phreeqc_ok}"
echo "[startup] phreeqc_database=${PHREEQC_DATABASE:-<unset>} found=${db_ok}"
echo "[startup] anthropic_key_present=${key_present}  (the key value is never logged)"
echo "[startup] serving on port ${PORT:-8501}"

exec streamlit run app.py \
    --server.port="${PORT:-8501}" \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
