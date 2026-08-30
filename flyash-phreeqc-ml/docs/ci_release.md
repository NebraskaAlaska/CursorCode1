# Phase 4 CI and release gate

`.github/workflows/ci.yml` is the required Phase 4 verification workflow. It keeps dependency,
host-Python, exact-image, Compose/security, and per-architecture evidence distinct. A release
candidate is not promoted merely because a subset passes.

Required jobs are:

- `python-and-release-contracts`;
- `dependency-vulnerability-audit`;
- `container-and-compose`;
- `PHREEQC integration (linux/amd64)` and `PHREEQC integration (linux/arm64)`;
- `Phase 4 required release gate`.

The container job performs a pull/no-cache test-target build, runs the complete suite with real
PHREEQC required, bootstraps pinned resources, runs the bundled official example, builds the final
release target, exercises hosted authentication fail-closed and simulated identity/tenant scoping,
and inspects the non-root runtime identity. Direct containers use `--init`, and pytest cache is
disabled because the image source tree is read-only. Compose app services use `init: true`.
The pinned USGS archive download retries bounded transient and partial-transfer failures, but the
fixed archive SHA-256 remains mandatory; retries cannot accept altered or incomplete bytes.
Runtime and development dependency installation likewise uses bounded retries with cache purge and
backoff; every version and available wheel hash remains constrained, so a retry never converts a
hash mismatch into acceptance.

Each architecture builds the exact test target and runs the runtime, executor, runner, provenance,
and Resource Steward integration contracts. QEMU setup is pinned to an immutable official action
commit. Neither architecture permits `continue-on-error`. The final `phase4-release-gate` runs with
`always()` and fails unless every required upstream result is `success`, so downstream evidence
skipped after an earlier failure cannot produce a green release gate.

Repository visibility and release promotion are separate. `NebraskaAlaska/CursorCode1` is
currently public, making all branches publicly readable. CI must never commit or upload real/private
research data, secrets, CEMDATA/unapproved database bytes, model weights, or generated scientific
outputs. Hosted user data remains outside Git. Visibility or branch-protection changes require a
separate owner decision; this workflow changes neither.
