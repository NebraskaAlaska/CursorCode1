# Controlled cross-computer Council operator

The Phase 5 operator is a trusted-host control plane for sending bounded WPI source tasks to an existing AI Council installation. It never treats model roles as trusted Git actors and never uses the live WPI checkout as a role workspace. Automatic completion ends at a deterministic task branch in `awaiting_human_review`; merge, deployment, and scientific-resource promotion remain separate human decisions.

The code repository is public. Every task branch is publicly readable. Submit only public or sanitized task metadata unless `control_remote` is a separately configured private repository, and never place credentials, unpublished research data, database bytes, model weights, generated scientific output, or raw role transcripts in a request or task branch.

## Trust and data flow

1. The personal computer seals a closed `wpi-council-task-request/v1` request and submits it to an append-only Git-ref state record in the configured control remote.
2. A worker acquires the task using a normal non-force Git push. The remote update is the compare-and-swap boundary: concurrent pushes cannot both advance the same state ref.
3. The trusted worker verifies the clean live checkout and the exact remote base commit, then creates a disposable clone outside the repository. It removes all remotes before any role sees a copy.
4. The compatibility adapter verifies SHA-256 identities for the external Council router, launcher, gates, exporter, prompts, and policy files. The Obsidian `AI Council` snapshot is explicitly rejected as a runtime.
5. The existing Council controls perform Router → Planner → Coder → coder gate → Tester → tester gate → Reviewer. A failed gate or review creates a hash-bound correction and a new immutable attempt.
6. Only the trusted operator can construct a commit and perform a normal push to `council/wpi/<task-id>`. Branch collisions, moved bases, forbidden paths, dirty live state, and non-fast-forward state updates fail closed.
7. The controller reads the resulting manifest and makes an explicit, exact-hash decision. Task-branch approval does not merge; PR, merge, deployment, resource-promotion, and rollback approvals are all different artifacts.

The external Council remains authoritative for routing, stage launching, role outboxes, coder/tester gates, and isolation. The project adapter calls those verified tools; it does not copy their logic into this repository.

## Configuration

Tracked policy is [`config/council_operator_policy.toml`](../config/council_operator_policy.toml). It binds the repository, permitted base branch, `council/wpi/` branch namespace, test-command prefixes, correction and resource limits, forbidden paths, approval classes, and exact Council contract hashes.

Copy [`config/council_operator.example.toml`](../config/council_operator.example.toml) to `~/.config/wpi-virtual-lab/council.toml`. This local file is untracked and contains machine paths and remote URLs but must not contain credentials. Supported runtime overrides include `WPI_AI_COUNCIL_ROOT`, `WPI_HERMES_EXECUTABLE`, `WPI_OBSIDIAN_VAULT`, `WPI_COUNCIL_POLICY`, `WPI_COUNCIL_REPOSITORY`, `WPI_COUNCIL_CODE_REPO`, `WPI_COUNCIL_CONTROL_REPO`, `WPI_COUNCIL_CONTROL_REPO_PRIVATE`, `WPI_COUNCIL_WORKER_ID`, `WPI_COUNCIL_SANDBOX_ROOT`, and `WPI_COUNCIL_STATE_CACHE_ROOT`; the bootstrap additionally accepts the absolute `WPI_COUNCIL_PYTHON`. Authentication comes from the user's existing Git credential helper or SSH configuration.

Installation resolves exactly Python 3.12 in this order: an absolute
`WPI_COUNCIL_PYTHON`, the checked-out project's `.venv/bin/python` when that
interpreter is 3.12, and an absolute `python3.12` found on `PATH`. Python 3.9,
3.10, 3.11, and 3.13 are rejected. If no accepted interpreter exists, the
resolver emits one installation instruction for creating the project's
`.venv` from an absolute Python 3.12 executable. The installer never changes
the system Python and never uses a user-site install inside a virtual
environment.

The first physical Phase 5-R1 Hermes installation attempt demonstrated that a
valid accepted Python 3.12 virtual environment can exist without importable
`pip`. The approved checkout and Council contract, all ten required Hermes
profiles, Docker, and the dependency-lock declaration passed, and the project
interpreter was Python 3.12.14 while the system `python3` remained Python
3.9.6. Installation then stopped at `No module named pip`. No worker doctor,
Council role, model invocation, or task submission occurred.

The R1 resolver selected exact Python 3.12 correctly, but the installer
assumed that an accepted pre-existing environment also contained pip. Its
functional Python 3.12 virtual-environment matrix did not include an explicit
`venv --without-pip` case. R1 also installed `requirements-dev.txt` before the
editable build without explicitly requesting the `setuptools==83.0.0` required
by both `pyproject.toml` and `constraints-py312.txt`; the exact CI image had
established that build requirement separately. R2 corrects these two aligned
bootstrap assumptions without changing dependency authority.

This correction changes only local operator bootstrap and build-toolchain
validation. Remote CAS locking, exact stale-state approval, owned worker
process groups and `caffeinate`, disposable role workspaces, non-mutating
remote push preflight, task-branch-only automatic push, human approval
separation, Resource Steward promotion/rollback authority, and all PHREEQC,
ICP, XRD, ML, evidence, and validation boundaries are unchanged.

For a new installation, the corrected installer gives the exact selected
operator interpreter to the single reusable
`scripts/bootstrap_council_pip.py` helper. The helper first
rechecks exact Python 3.12 and then tests `python -m pip --version`. A working
`pip` is preserved without an upgrade. If and only if `pip` is missing, that
same interpreter runs its standard-library `ensurepip --default-pip`; no other
Python, `sudo`, `--user`, activation, environment recreation, downloaded
`get-pip.py`, `curl` bootstrap, or remote executable bootstrap is permitted.
The helper then proves that `python -m pip` works and that the pip module and
distribution belong to the selected environment, not the system Python or
user site. Its bounded result records the interpreter, prefix, pip version and
locations, and whether bootstrap occurred. Missing or failed `ensurepip` stops
with one deterministic corrective instruction. A present but broken or
externally resolved pip fails closed and is not reclassified as missing to
trigger ensurepip.

Before the editable installation, the installer explicitly establishes the
build requirement declared by `pyproject.toml` using the same exact
constraints authority as the runtime and development dependencies. Its
dependency operation is equivalent to:

```bash
python -m pip install \
  --constraint constraints-py312.txt \
  setuptools==83.0.0 \
  --requirement requirements-dev.txt
python -m pip install \
  --no-build-isolation \
  --no-deps \
  --editable /absolute/path/to/flyash-phreeqc-ml
```

Validation requires Python 3.12, `setuptools==83.0.0`, every runtime pin, the
pytest development pin, the editable package resolving to the intended
checkout, required imports, and `pip check`. `--no-build-isolation` and the
tracked exact pins remain mandatory; neither an ambient nor an unconstrained
setuptools satisfies the contract.

When the selected interpreter is already a virtual environment, the package
is installed there. When it is a base interpreter, the installer creates a
dedicated environment under the user-local operator installation root. It
records the absolute Python path, exact 3.12 version, executable SHA-256, and
project path in a mode-`0600` runtime record. A stable `wpi-council` launcher
verifies that record on every invocation, so routine commands work in a new
terminal without activating an environment. A valid existing installation is
preserved; a missing, altered, or incompatible runtime record or environment
fails closed instead of being replaced silently.
Config, runtime-record, and launcher directory chains must stay under the
configured user home and be real, current-user-owned, and not group- or
world-writable; shared writable or symlinked installation parents are refused.

Safe empty directories are not an incompatible installation. A prior stopped
attempt may leave an empty config parent, an empty install root, or an existing
`~/.local/bin`; the corrected installer validates and reuses that shape without
requiring deletion. Existing config, runtime records, installed resolvers,
launchers, symlinks, or environments still receive the stricter compatibility
checks. For a new installation, config, runtime record, installed resolver,
and stable launcher publication occur only after pip bootstrap, dependency and
build-tool installation, editable installation, and every installed-runtime
validation succeed. Bootstrap or installation failure removes temporary files
and any newly created incomplete dedicated environment, and publishes none of
those operator-state artifacts.

Use a private `control_remote` for private state metadata. The operator does not create that repository or infer its visibility. Private/confidential classification is enabled only when a separate remote is configured and `control_remote_private = true` is explicitly attested after verifying its visibility. With an unattested, public, or shared control remote, only public or sanitized requests are accepted and secret/research-content scans still run.

## Request contract

Requests are UTF-8 Markdown containing one canonical JSON envelope, or a JSON object, with no unknown fields. The JSON Schema is [`resources/schemas/council-task-request.schema.json`](../resources/schemas/council-task-request.schema.json). The request binds the exact repository, branch, commit, path allow/deny lists, acceptance criteria, argument-array tests, scientific/privacy risks, external resources, backend, limits, requester, time, and canonical SHA-256.

Create and submit a request from the personal computer:

```bash
wpi-council doctor
wpi-council create-request REQUEST.md \
  --task-id docs-example \
  --title "Clarify one public document" \
  --goal "Make the requested documentation-only correction" \
  --background "Public and sanitized maintenance request" \
  --allowed-path 'docs/**' \
  --acceptance "Focused tests and the complete suite pass" \
  --requester 'human:maintainer' \
  --backend hermes
wpi-council submit REQUEST.md
```

Once claimed, the request object and hash are immutable. A changed request is a new revision with a new canonical hash and must be submitted as a new state revision before a worker claim.

## State and remote lock

Each task has one ref under `refs/heads/council-state/<project-slug>/<task-id>` in the control remote. Its commit contains one closed `wpi-council-task-state/v1` record and append-only event/evidence history. Updates fetch the observed ref, create a successor commit, and push without `--force`; a competing winner makes the loser retry or stop. No server-side custom hook is assumed.

The states are:

```text
draft → submitted → claimed → routing → planning → coding → coder_gate
      → testing → tester_gate → reviewing → automatic_gates_passed
      → task_branch_pushed → awaiting_human_review → approved | rejected
```

`correction_required` may return to a new `routing` attempt. `expired`, `failed`, and `cancelled` are explicit bounded stops. Transitions are allowlisted; there is no silent backward transition. Heartbeats update only lease metadata while the task remains in an active owned state; an explicitly `expired` state rejects the previous worker's heartbeat so its identity is frozen for review. Release is idempotent at the safe `submitted` boundary. Stale leases remain visible, and takeover requires either policy-enabled expiry or a separate human override. Under the default policy, that approval is created only after explicit expiry and binds the task ID, request hash, base commit, canonical SHA-256 of the complete expired state, expired-state revision, previous worker ID, exact lease expiry, exact expiry transition time, approval time, and approving human. Claim re-reads the current remote state and compares every binding before its normal non-force compare-and-swap update. A heartbeat, timestamp or revision edit, state change, new expiry cycle, different worker/request/base, future or pre-expiry approval, or a second use after successful takeover is rejected.

## Disposable workspaces and role isolation

The sandbox manager refuses a dirty live checkout, wrong branch, wrong repository identity, stale remote base, symlinked root, traversal, or an existing task directory. It creates a no-hardlink clone at the exact base, records the starting tree hash, removes Git remotes, and gives roles further private copies. Role environments omit GitHub/model tokens, SSH agent sockets, credential helpers, and the user's home. Processes use argument arrays, bounded output, timeouts, and process-group termination.

Failed attempts are retained as immutable evidence; attempt identifiers cannot be reused. Cleanup is not automatic. The trusted host alone applies gate-approved patches, creates a bounded commit, and pushes a new task branch. It never switches, stashes, resets, or writes the live checkout.

## Backends and correction loop

`hermes` invokes the verified Council stage launcher, which selects the configured Planner/Coder profile for the deterministic route level and the separate Tester/Reviewer profiles. Worker preflight requires all ten launcher-selected profiles and hashes each profile's `config.yaml`, `SOUL.md`, and `state.db` without recording their contents. Invocation artifacts retain profile/config identities as hashes.

`command` is a future approved-CLI adapter. Every stage is an argument array and the resolved executable is bound to an exact SHA-256. It uses the same Council outboxes and deterministic ingestion/gates. No Codex CLI contract is invented.

`fake` is synthetic test-only evidence. It exercises success, malformed/adversarial output, timeouts, repeated failure, and Coder/Tester/Reviewer correction journeys; it is never represented as a live Council run.

Each correction binds the previous patch, test, and reviewer evidence hashes. A correction always gets a new immutable attempt directory. Policy caps rounds, duration, invocations, changed files, patch bytes, and logs. Repeated failures, path expansion, scientific authority changes, real-data requirements, malformed output, and all limits stop for a human rather than retrying indefinitely.

## Operating commands

The stable launcher and every operator shell entry point verify the runtime
record before loading the operator; the loaded operator independently requires
exactly Python 3.12 and captures its executable identity. After the Council
tester gate, an immutable requested test whose first argument is `python` or
`python3` is executed with that exact operator/test interpreter. The original
argument array and effective argument array are both retained, along with
interpreter path, version, SHA-256, environment prefix, base prefix,
implementation, virtual-environment status, and the SHA-256 of the complete
tracked dependency contract. Doctor, task run, and trusted-test execution each
revalidate that contract, every active installed pin, and `pip check` through
the recorded interpreter before proceeding. Execution remains an argument
array in the disposable workspace with the credential-stripped environment;
no shell activation or interpolation is introduced. Other executables remain
subject to the tracked prefix allowlist.

`doctor` checks more than remote readability. For both the code remote and
control remote it performs `git ls-remote`, then a Git push dry run from the
exact local commit to a fresh nonexistent ref in the required namespace:
`council/wpi/permission-preflight-*` for code and
`council-state/<project>/permission-preflight-*` for control. It confirms the
probe ref is absent before and after. The bounded report distinguishes read
success, dry-run push authorization, authentication failure, network failure,
and branch/ref-policy refusal without returning the remote error text or URL.
Both read and write checks must pass before any backend or model stage is
constructed.

Personal computer:

```bash
wpi-council doctor
wpi-council submit REQUEST.md
wpi-council status <task-id>
wpi-council watch <task-id>
wpi-council review <task-id> --output REVIEW.json
wpi-council approve-task-branch <task-id> --approved-by human:<name>
wpi-council reject <task-id> --actor human:<name> --reason '<reason>'
wpi-council sync-handoff <task-id> --expected-before-hash <sha256>
```

Hermes worker:

```bash
wpi-council doctor --worker
wpi-council claim <task-id>
wpi-council heartbeat <task-id>
wpi-council run <task-id> --backend hermes
wpi-council resume <task-id>
wpi-council release <task-id>
```

Stale recovery is deliberately two-person-shaped and explicit under the default policy:

```bash
wpi-council expire <task-id> --actor human:<name>
wpi-council create-stale-lock-approval <task-id> --approved-by human:<name> --output STALE_APPROVAL.json
wpi-council claim <task-id> --stale-approval STALE_APPROVAL.json
wpi-council resume <task-id>
```

An interrupted or partially written attempt directory is retained and recorded as failed evidence. Resume always allocates a new attempt ID and consumes the bounded correction budget.

The local CLI is the status authority; no scientist-facing admin surface is enabled. `status` and `watch` disclose only bounded state/evidence metadata, not unrestricted role output.

For background operation, `start-hermes-worker.sh` and
`stop-hermes-worker.sh` use the same recorded Python runtime as the launcher.
Start associates one task with a random worker-instance identity, starts a
supervisor and worker in dedicated sessions/process groups, and writes strict
atomic mode-`0600` JSON metadata. That metadata binds the requested and
effective command hashes, operator Python identity, kernel-derived process
start identities, executable hashes, process group/session IDs, stop
capability, and sleep assertion. Malformed, symlinked, wrong-mode, PID-only,
PID-reused, or unrelated metadata is refused rather than signalled.

On macOS the worker command is held under the verified absolute
`/usr/bin/caffeinate -dimsu`; the assertion ends with the owned worker process.
The public `--overnight` mode is accepted only inside that owned supervisor
instance; a raw foreground invocation fails before remote preflight or model
work. Use `start-hermes-worker.sh` for every unattended or overnight run.
Stop is delivered to the owning supervisor, which re-verifies process identity,
sends SIGTERM to the complete owned worker process group, waits a bounded
period, sends SIGKILL only if that same group remains owned, and reaps the
child. Local active metadata is removed only after verified shutdown. A
finished worker is reported as already finished, while the configured control
remote remains the source of truth for task evidence and lease recovery.

## Evidence and approvals

`review` returns the request/base/task identities, route, worker, task branch and commit, patch/test/reviewer hashes, and automatic-gate outcome. Inspect the actual task-branch diff and run the required tests before approval.

Approval commands are deliberately separate:

```bash
wpi-council approve-task-branch <task-id> --approved-by human:<name>
wpi-council approve-pull-request <task-id> --approved-by human:<name>
wpi-council approve-merge <task-id> --approved-by human:<name>
wpi-council approve-deployment <task-id> --approved-by human:<name>
wpi-council approve-resource-promotion <task-id> --approved-by human:<name> --proposal-id <id> --candidate-sha256 <sha256>
wpi-council approve-resource-rollback <task-id> --approved-by human:<name> --proposal-id <id> --candidate-sha256 <sha256>
```

Every completion approval binds the current request, base, final commit, patch,
tests, and reviewer identities. Stale-lock takeover uses its stricter exact
expired-state binding described above and is recorded as consumed in the
successful successor state. Stale, forged, modified, or replayed artifacts
fail. Reviewer output can never act as human approval.

The Resource Steward adapter delegates to the existing Phase 4 deterministic CLI. `check`, `show`, `download-candidate`, `verify-candidate`, `build-candidate`, `test-candidate`, `compare`, and `export-report` may run automatically. `promote` and `rollback` always require their own exact human artifact; task or merge approval is insufficient.

## Recovery and troubleshooting

- `doctor` failing on a dirty live checkout: finish or remove local changes yourself; the operator never stashes or resets them.
- Base reported stale: revise the request against the exact current remote commit. Do not mutate a claimed request.
- Claim collision: inspect `status`. Only the remote state-ref winner owns the lease.
- Interrupted process: inspect `status`, preserve the attempt directory, then use `resume` at a supported attempt boundary. Do not reuse a Council run ID.
- Stale lease: explicitly expire it before creating the exact human takeover approval. The expired state rejects further heartbeats and remains frozen for binding; default policy disables automatic takeover.
- Task branch already exists or moved: stop and review it; the operator will not force-push or overwrite it.
- Contract/profile hash mismatch: compare the configured live Council/Hermes installation with the audited contract. Produce and separately approve a compatibility update; never patch the live Council automatically.
- Network failure: no state is inferred from local intent. Re-read the remote state ref and code branch before retrying.
- Remote permission failure: inspect the bounded `doctor` failure class. Readability alone is insufficient; both the ordinary task-branch namespace and control state-ref namespace must pass the non-mutating push dry run before model stages can begin.
- Worker stop refusal: retain the metadata and inspect it. A malformed record, reused PID, unrelated process identity, or legacy PID-only file is deliberately never treated as authority to send a signal. Remote lease recovery remains a separate operator action.
- Installer reports missing `pip`: use the corrected installer with the same
  selected Python 3.12. Do not run `get-pip.py`, curl executable bootstrap
  content, activate another environment, or fall back to system Python. A
  missing or failed standard-library `ensurepip` is a fail-closed host
  prerequisite, not permission to weaken the bootstrap contract.
- A stopped installer left only empty user-local parents: leave those safe
  directories in place and rerun the corrected installer. Do not delete them.
  A config, runtime record, installed resolver, launcher, symlink, or dedicated
  environment is different state and must pass the documented compatibility
  checks rather than being overwritten.
- Handoff conflict: re-read the Obsidian note, calculate its current SHA-256, and explicitly retry `sync-handoff`; synchronization only appends sanitized summaries.

See [`hermes_operator_installation.md`](hermes_operator_installation.md) for
the corrected worker bootstrap and
[`hermes_physical_acceptance.md`](hermes_physical_acceptance.md) for the
controlled physical acceptance procedure. The first physical attempt stopped
safely inside installation before worker doctor, model invocation, or task
submission. Acceptance remains incomplete; the R2 repository correction does
not resume it.
