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

## Phase 5 operator gate

The same workflow also contains `Phase 5 Council operator contracts` and the
final `Phase 5 required release gate`. The operator job functionally exercises
the Python 3.12 bootstrap in temporary environments, rejection of incompatible
Python, the stable launcher from a clean shell, exact trusted-test interpreter
rewriting and installed dependency revalidation, exact-state stale-approval
consumption/replay rejection, safe worker process-group supervision and
PID-reuse refusal, and non-mutating code/control remote permission probes
against local bare repositories. These checks are in addition to the existing
request, lock/CAS, disposable-workspace, Council adapter/gate, privacy,
approval, correction-loop, Resource Steward refusal, successful-task, and
two-clone journeys; shell syntax checks alone are not acceptance evidence.

Phase 5-R2 adds a functional pipless-interpreter correction to that same job.
Exact Phase 5-R1 run `33406298103` remains the green historical baseline; its
installer fixture used a pip-equipped Python 3.12 environment and therefore
did not expose the physical pipless-venv case. R2 adds that missing case rather
than reinterpreting the earlier result.
`tests/test_council_operator_installation_phase5_r2.py` creates a genuine
Python 3.12 virtual environment with `venv --without-pip`, proves pip is
initially unavailable, and runs the trusted bootstrap and installer path. The
test requires same-interpreter standard-library `ensurepip`, verifies the pip
module and distribution are under that exact environment prefix, and proves a
simulated system Python 3.9 cannot satisfy or perform bootstrap. A separate
present-pip case requires `bootstrapped: false` and preserves usable pip
without invoking ensurepip unnecessarily. Source-text inspection is not a
substitute for these executions.

The R2 matrix also explicitly installs and verifies constrained
`setuptools==83.0.0`, retains `--no-build-isolation --no-deps` for the editable
package, rejects another setuptools version, verifies all runtime and pytest
pins, editable checkout identity, imports, and `pip check`, and exercises the
real stopped-attempt directory shape: safe empty config/install parents plus
an existing user-local bin directory. Failure injection at ensurepip,
dependency installation, setuptools verification, editable installation, and
installed-pin validation must leave config, runtime record, installed
resolver, and internal/stable launchers unpublished and remove temporary
files.

After the functional pytest step succeeds, CI emits these bounded proof
markers:

```text
PIPLESS_PY312_BOOTSTRAP_PASS=True
ENSUREPIP_IS_LOCAL_AND_TRUSTED=True
SYSTEM_PYTHON_NOT_USED=True
PINNED_SETUPTOOLS_83_INSTALLED=True
PARTIAL_INSTALL_RECOVERY_PASS=True
FAILED_BOOTSTRAP_PUBLISHES_NO_OPERATOR_STATE=True
```

The first physical Hermes acceptance attempt is separate evidence: it reached
installation with Python 3.12.14 but no pip and stopped before worker doctor,
model invocation, or task submission. Neither the new functional CI fixture
nor that safely stopped installation is a completed live Hermes acceptance.

The Phase 5 release gate runs with `always()` and requires both the Phase 4
aggregate release gate and Phase 5 operator job to be `success`. A green Phase
5 gate therefore preserves the scientific/resource/runtime boundary rather
than replacing it. CI and local simulations do not prove that a live Hermes
four-role task has run; physical evidence is recorded only by the separate
[`hermes_physical_acceptance.md`](hermes_physical_acceptance.md) procedure.
The R2 additions do not remove, skip, or weaken any required Phase 4 job,
architecture slice, exact-image check, or either aggregate release gate.
