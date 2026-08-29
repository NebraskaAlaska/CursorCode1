#!/usr/bin/env sh
# Fail-closed container startup. It verifies immutable bundled runtime identity,
# checks durable mounts, restores only a blank template, and logs no secret value
# or user content.
set -eu

umask 027

sha256_file() {
    sha256sum "$1" | awk '{print $1}'
}

require_writable_dir() {
    path="$1"
    mkdir -p "$path" 2>/dev/null || {
        echo "[startup:error] required durable directory cannot be created: $path" >&2
        exit 70
    }
    if [ ! -w "$path" ]; then
        echo "[startup:error] required durable directory is not writable by uid $(id -u): $path" >&2
        exit 70
    fi
}

has_allowlist_entry() {
    # Match the application's comma/semicolon parser closely enough to reject
    # values containing only separators or whitespace at startup.
    compact=$(printf '%s' "${1:-}" | tr -d '[:space:],;')
    [ -n "$compact" ]
}

for durable_dir in \
    /app/outputs \
    /app/experiments \
    /app/data/processed \
    /app/imports/experimental_icp \
    "${WPI_RESOURCE_ROOT:-/var/lib/wpi/resources}"
do
    require_writable_dir "$durable_dir"
done

template=/opt/wpi/templates/SYNTHETIC_DEMO_icp_template.csv
template_dest=/app/imports/experimental_icp/experimental_release_template.csv
if [ ! -f "$template_dest" ]; then
    cp "$template" "$template_dest"
fi

runtime_manifest=${PHREEQC_RUNTIME_MANIFEST:-/opt/phreeqc/share/manifest/runtime-manifest.json}
rights_notice=/opt/phreeqc/share/doc/phreeqc/USGS_USER_RIGHTS_NOTICE.txt
exe_path=${PHREEQC_EXE:-/opt/phreeqc/bin/phreeqc}
database_path=${PHREEQC_DATABASE:-/opt/phreeqc/database/phreeqc.dat}

for required_file in "$runtime_manifest" "$rights_notice" "$exe_path" "$database_path"; do
    if [ ! -f "$required_file" ]; then
        echo "[startup:error] required runtime file is missing: $required_file" >&2
        exit 78
    fi
done
if [ ! -x "$exe_path" ]; then
    echo "[startup:error] PHREEQC executable is not executable" >&2
    exit 78
fi

database_sha=$(sha256_file "$database_path")
if [ "$database_path" = /opt/phreeqc/database/phreeqc.dat ] \
   && [ "$database_sha" != "$PHREEQC_DATABASE_SHA256" ]; then
    echo "[startup:error] bundled phreeqc.dat hash mismatch" >&2
    exit 78
fi

resource_root=${WPI_RESOURCE_ROOT:-/var/lib/wpi/resources}
bootstrap_result="$resource_root/release-bootstrap-result.json"
bootstrap_temporary="$resource_root/.release-bootstrap-result.$$"
if ! python -m flyash_phreeqc_ml.resource_steward \
    --store-root "$resource_root" \
    bootstrap-release \
    --runtime-manifest "$runtime_manifest" \
    --executable "$exe_path" \
    --database-dir /opt/phreeqc/database \
    --registry /app/resources/official_usgs_3.8.6-17100_databases.json \
    --knowledge-output-dir "$resource_root/knowledge" \
    >"$bootstrap_temporary"
then
    rm -f "$bootstrap_temporary"
    echo "[startup:error] bundled scientific-resource bootstrap failed" >&2
    exit 78
fi
mv "$bootstrap_temporary" "$bootstrap_result"

identity_line=$(python -c \
    'import json,sys; d=json.load(open(sys.argv[1], encoding="utf-8")); print("|".join((d["runtime_installation_id"], d["active_database_installation_id"], d["knowledge_pack_hash"])))' \
    "$bootstrap_result")
IFS='|' read -r active_runtime_id active_database_id knowledge_pack_hash <<EOF
$identity_line
EOF
export VLAB_RESOURCE_CATALOG="$resource_root/catalog.json"
export VLAB_RESOURCE_BOOTSTRAP_RESULT="$bootstrap_result"
export VLAB_ACTIVE_RUNTIME_INSTALLATION_ID="$active_runtime_id"
export VLAB_ACTIVE_DATABASE_INSTALLATION_ID="$active_database_id"
export VLAB_KNOWLEDGE_PACK_HASH="$knowledge_pack_hash"

if [ "${VLAB_DEPLOYMENT_MODE:-local}" = hosted ]; then
    if [ "${WPI_AUTH_ENFORCED:-}" != true ]; then
        echo "[startup:error] hosted mode requires an authenticated reverse proxy" >&2
        exit 78
    fi
    if [ ! -r /app/.streamlit/secrets.toml ]; then
        echo "[startup:error] hosted mode requires mounted Streamlit OIDC secrets" >&2
        exit 78
    fi
    if ! has_allowlist_entry "${VLAB_ALLOWED_SUBJECTS:-}" \
       && ! has_allowlist_entry "${VLAB_ALLOWED_TENANTS:-}"; then
        echo "[startup:error] hosted mode requires a subject or tenant invite allow-list" >&2
        exit 78
    fi
    if ! has_allowlist_entry "${VLAB_ADMIN_SUBJECTS:-}" \
       && ! has_allowlist_entry "${VLAB_MEMBER_SUBJECTS:-}" \
       && ! has_allowlist_entry "${VLAB_MEMBER_TENANTS:-}" \
       && ! has_allowlist_entry "${VLAB_VIEWER_SUBJECTS:-}" \
       && ! has_allowlist_entry "${VLAB_VIEWER_TENANTS:-}"; then
        echo "[startup:error] hosted mode requires a separate viewer/member/admin role allow-list" >&2
        exit 78
    fi
fi

key_present=false
if [ -n "${VLAB_AI_API_KEY:-${ANTHROPIC_API_KEY:-}}" ]; then key_present=true; fi

echo "[startup] app_version=${APP_VERSION:-unknown} vcs_ref=${APP_VCS_REF:-unknown} mode=${VLAB_DEPLOYMENT_MODE:-local}"
echo "[startup] runtime_id=${PHREEQC_RUNTIME_ID:-unknown} phreeqc_version=${PHREEQC_VERSION:-unknown} executable_sha256=$(sha256_file "$exe_path")"
echo "[startup] database_id=${PHREEQC_DATABASE_ID:-external-or-unknown} database_sha256=$database_sha"
echo "[startup] runtime_manifest=$runtime_manifest source_manifest=${PHREEQC_SOURCE_MANIFEST:-unknown}"
echo "[startup] resource_catalog_generation_verified=true knowledge_pack_hash=$knowledge_pack_hash"
echo "[startup] ai_provider=${VLAB_AI_PROVIDER:-disabled} cloud_key_present=$key_present"
echo "[startup] scientific_status=simulation_runtime_not_experimental_validation port=${PORT:-8501}"

exec streamlit run app.py \
    --server.port="${PORT:-8501}" \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
