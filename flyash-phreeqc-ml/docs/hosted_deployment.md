# Invite-only hosted deployment

This bundle is an invite-only scientific beta, not a public SaaS deployment. No public URL or live identity-provider login is claimed by the Phase 4 release evidence. Unit/AppTest coverage verifies OIDC claim expiry, invite/role policy, anonymous fail-closed behavior, tenant roots, guessed IDs, cross-tenant concurrency, and hosted legacy-path blocking. A real provider login/logout drill remains a deployment-operator acceptance step because no deployment credentials were supplied.

## Architecture and trust boundaries

- Nginx alone publishes HTTPS on port 8443. Streamlit port 8501 and optional Ollama port 11434 are not published.
- Streamlit uses current `st.login`, `st.user`, and `st.logout` OIDC integration. The mounted secrets file is outside Git and the image.
- OIDC proves identity only. Invite and application roles are separate operator-controlled allowlists. An identity-provider `roles` claim cannot grant application access.
- Durable project/material/run/artifact storage is hashed by tenant; active context is additionally hashed by subject. Raw subject/tenant claims never become path components.
- Legacy global Validate, Match, Compare, validation-report, and shared pipeline surfaces return before any global reader/action in hosted mode. Tenant-scoped durable workflows remain available.
- The app runs as UID 10001 and the TLS proxy as UID 101. Both use read-only root filesystems and drop all capabilities; Compose also bounds CPU/memory/PIDs/files, upload/rate limits, and the five durable app volumes.
- AI is disabled by default. A hosted endpoint/model is administrator-managed; endpoint and credential values are not shown to users.

## Required operator inputs

Create the OIDC secrets, htpasswd, TLS certificate, and key outside the repository. Start from `deploy/streamlit-oidc-secrets.toml.example`; set an exact HTTPS `redirect_uri`, random `cookie_secret`, provider `client_id`/`client_secret`, and `server_metadata_url`. The proxy runs as numeric UID/GID `101:101`, so the mounted certificate, private key, and htpasswd must be readable by UID 101 through controlled ownership or an ACL on the deployment host; do not make those files world-readable.

Set paths and opaque allowlists without printing secret values:

```bash
export WPI_AUTH_FILE=/secure/wpi/auth.htpasswd
export WPI_TLS_CERT_FILE=/secure/wpi/fullchain.pem
export WPI_TLS_KEY_FILE=/secure/wpi/private-key.pem
export VLAB_STREAMLIT_SECRETS_FILE=/secure/wpi/streamlit-oidc-secrets.toml

export VLAB_ALLOWED_SUBJECTS=invited-opaque-subject-a,invited-opaque-subject-b
export VLAB_ALLOWED_TENANTS=
export VLAB_ADMIN_SUBJECTS=invited-opaque-subject-a
export VLAB_MEMBER_SUBJECTS=invited-opaque-subject-b
export VLAB_MEMBER_TENANTS=
export VLAB_VIEWER_SUBJECTS=
export VLAB_VIEWER_TENANTS=
export VLAB_OIDC_TENANT_CLAIM=sub

export WPI_IMAGE=wpi-virtual-lab:phase4-rc
export WPI_IMAGE_DIGEST=
```

At least one subject/tenant invite allowlist and at least one viewer/member/admin role allowlist are mandatory. Prefer a stable organization/tenant claim only if the provider actually supplies one. `WPI_IMAGE_DIGEST` stays empty for an unverified local build; after registry publication it must be the exact observed OCI digest, not a tag or placeholder.

## Build and local staging

From the package directory:

```bash
docker compose -f docker-compose.server.yml config --quiet
docker build --pull --target test -t wpi-virtual-lab:phase4-test .
docker run --rm --entrypoint python wpi-virtual-lab:phase4-test \
  -m pytest -q -p no:cacheprovider
docker compose -f docker-compose.server.yml build --pull app
docker compose -f docker-compose.server.yml up -d
docker compose -f docker-compose.server.yml ps
```

The release target intentionally excludes test sources and development dependencies;
run the full suite in the `test` target before starting the release target.

Verify the Nginx health configuration and the Streamlit health endpoint from inside the private network. From the operator host, an unauthenticated HTTPS request must receive the configured authentication challenge and must never reach an upload/store surface. Then perform two real browser sessions through the configured IdP: invited member A, invited member B/another tenant, logout/login, guessed IDs, upload isolation, project export, and role restrictions. Record the identity-provider name, date, image digest, exact commands, and actual result without recording claims/tokens/cookies. If credentials are unavailable, record this live-provider drill as `not_run`; do not call unit-simulated `st.user` coverage a live OIDC success.

## TLS, proxy, limits, and logs

`deploy/nginx.conf` enables TLS 1.2/1.3, WebSocket forwarding, a 25 MiB body cap, request rate limiting, security headers, and a minimal access log. Basic auth is defense in depth, not application identity. Replace the example certificate and htpasswd on a controlled host; restrict their filesystem permissions and rotation access. Compose starts Nginx directly as UID/GID 101 with identity-owned temporary filesystems, a read-only image root, `no-new-privileges`, and all Linux capabilities dropped.

Compose defaults cap the app at 2 CPUs, 4 GiB memory, 256 processes, 4096/8192 file descriptors, and PHREEQC at 120 seconds. Tune downward for the beta workload and rerun concurrency/timeout tests. Logs contain method/path/status and bounded safe diagnostics; they must not include prompts, uploaded scientific values, credentials, tokens, cookies, or raw OIDC claims. Route platform logs to a restricted retention policy.

## Database administrator workflow

Ordinary hosted users see scientific resources read-only. A human administrator may use **Settings & Diagnostics → Database Manager** to upload one reviewed official/user-supplied `.dat` or one exact `.dat` archive member. They must record source, version, archive/member hashes, citation, and rights basis. Import is side-by-side and inactive; extensions require their declared base and are never concatenated.

For a PHREEQC runtime/official database update, use the deterministic Steward inside the app container. Replace example metadata only with reviewed official-source evidence:

```bash
docker compose -f docker-compose.server.yml exec -T app \
  python -m flyash_phreeqc_ml.resource_steward --store-root /var/lib/wpi/resources \
  check --resource-id usgs.phreeqc.runtime --kind phreeqc_runtime \
  --candidate-version VERSION --source-url EXACT_OFFICIAL_HTTPS_ARCHIVE \
  --archive-filename EXACT_FILENAME --expected-sha256 EXACT_SHA256 \
  --rights-notice 'reviewed official notice location' --redistribution-state permitted

docker compose -f docker-compose.server.yml exec -T app \
  python -m flyash_phreeqc_ml.resource_steward --store-root /var/lib/wpi/resources \
  download-candidate PROPOSAL_ID
docker compose -f docker-compose.server.yml exec -T app \
  python -m flyash_phreeqc_ml.resource_steward --store-root /var/lib/wpi/resources \
  verify-candidate PROPOSAL_ID
docker compose -f docker-compose.server.yml exec -T app \
  python -m flyash_phreeqc_ml.resource_steward --store-root /var/lib/wpi/resources \
  build-candidate PROPOSAL_ID
docker compose -f docker-compose.server.yml exec -T app \
  python -m flyash_phreeqc_ml.resource_steward --store-root /var/lib/wpi/resources \
  test-candidate PROPOSAL_ID
docker compose -f docker-compose.server.yml exec -T app \
  python -m flyash_phreeqc_ml.resource_steward --store-root /var/lib/wpi/resources \
  compare PROPOSAL_ID
```

Export and review the JSON/human report. Promotion requires the exact proposal ID, candidate hash, complete built-in tests, no unresolved high/medium finding, and the exact human administrator confirmation. The scheduled monitor and an LLM cannot promote. CEMDATA remains `external_only`: the app does not redistribute it or infer a download URL from an unreadable official page.

## Backup and restore

Back up all five durable roots to an operator-controlled encrypted destination. The helper briefly stops and restarts the app so the five volumes are captured without concurrent application writes. It creates an unencrypted tar archive plus a mandatory SHA-256 sidecar; schedule a maintenance window and encrypt/restrict both files immediately:

```bash
export WPI_COMPOSE_FILE=docker-compose.server.yml
./scripts/backup-local.sh /secure/wpi/backups
sha256sum -c /secure/wpi/backups/ARCHIVE.tar.gz.sha256
```

Run a restore drill only on a disposable staging stack with the same image digest. The helper validates the checksum, rejects links/special/traversal/unexpected archive paths, stops the app, clears and restores only the five exact allowlisted roots, and restarts it. This is a replacement restore rather than an overlay, so take and verify a separate safety backup before using it on any non-disposable stack:

```bash
export WPI_COMPOSE_FILE=docker-compose.server.yml
./scripts/restore-local.sh --yes /secure/wpi/backups/ARCHIVE.tar.gz
docker compose -f docker-compose.server.yml ps
```

After restore, verify catalog/knowledge hashes, project counts, PHREEQC provenance, and two-tenant isolation. A backup is private research data, not a release artifact. If archive validation, app shutdown, extraction, or restart fails, treat the restore as failed and keep the stack unavailable until an operator has inspected the five durable roots; never continue research use from a partially restored stack.

PowerShell helpers honor the same selector via `$env:WPI_COMPOSE_FILE = "docker-compose.server.yml"` before invoking `backup-local.ps1` or `restore-local.ps1`.

## Export and deletion

Every project card exposes a deterministic tenant-scoped JSON export with an `export_sha256`. It includes project, material, run, and artifact records. It does not dereference external result paths and never includes session-only AI conversation history or credentials.

Permanent project deletion is administrator-only, requires the project to be archived, and requires typing `DELETE <project_id> <current_export_sha256>`. The store rechecks the current export hash, clears tenant user contexts pointing at the project, and deletes only exact project/material/run/artifact JSON paths. It does not guess/delete external or legacy files; operators must separately inventory them under their documented retention policy. The deletion is irreversible.

**Settings & Diagnostics → Account data and deletion** deletes only the current subject's persisted active-context file after `DELETE ACCOUNT <opaque-id>`. AI history is session-only. Tenant scientific projects remain because they can be shared with other investigators. The operator must separately remove/disable the identity-provider account and review platform logs/backups under the retention policy.

## Upgrade and rollback

1. Record the current immutable image digest, resource catalog/knowledge hashes, and a verified backup.
2. Build/scan/test the candidate image for both target architectures. A new PHREEQC/database is a separate Steward proposal; never treat an image tag change as scientific-resource approval.
3. Render Compose and boot a disposable staging copy with restored test data. Run full in-image tests, PHREEQC integration, OIDC/member/admin journeys, persistence, limits, export, and backup/restore.
4. Pin `WPI_IMAGE` to the accepted digest, set `WPI_IMAGE_DIGEST` to that same observed digest, and run `docker compose -f docker-compose.server.yml up -d --no-build`.
5. If health or acceptance fails, restore the previous image digest with the unchanged volumes. If a scientific resource was explicitly promoted, use the Steward's exact-confirmation rollback so evidence remains preserved; do not edit the catalog manually.
6. Do not delete the backup or previous image until post-upgrade acceptance is recorded. Do not publish or invite users based on code review alone.

## Acceptance record

Before invitations, record actual results—not expectations—for: image digest/platform, full in-image suite, 32 upstream examples, real project executor integration, PHREEQC hashes/identity, Compose health/restart, unauthenticated rejection, real OIDC success/logout, member/admin roles, two-tenant isolation, upload/AI isolation, resource limits, backup/restore, export/deletion on disposable records, and upgrade/rollback. Any unavailable live credential-dependent check remains explicitly `not_run`.
