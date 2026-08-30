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

Copy [`config/council_operator.example.toml`](../config/council_operator.example.toml) to `~/.config/wpi-virtual-lab/council.toml`. This local file is untracked and contains machine paths and remote URLs but must not contain credentials. Supported overrides include `WPI_AI_COUNCIL_ROOT`, `WPI_COUNCIL_POLICY`, `WPI_COUNCIL_REPOSITORY`, `WPI_COUNCIL_CODE_REPO`, `WPI_COUNCIL_CONTROL_REPO`, `WPI_COUNCIL_CONTROL_REPO_PRIVATE`, `WPI_COUNCIL_WORKER_ID`, `WPI_COUNCIL_SANDBOX_ROOT`, and `WPI_COUNCIL_STATE_CACHE_ROOT`. Authentication comes from the user's existing Git credential helper or SSH configuration.

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

`correction_required` may return to a new `routing` attempt. `expired`, `failed`, and `cancelled` are explicit bounded stops. Transitions are allowlisted; there is no silent backward transition. Heartbeats update only lease metadata. Release is idempotent at the safe `submitted` boundary. Stale leases remain visible, and takeover requires either policy-enabled expiry or a separate human override bound to the exact state hash.

## Disposable workspaces and role isolation

The sandbox manager refuses a dirty live checkout, wrong branch, wrong repository identity, stale remote base, symlinked root, traversal, or an existing task directory. It creates a no-hardlink clone at the exact base, records the starting tree hash, removes Git remotes, and gives roles further private copies. Role environments omit GitHub/model tokens, SSH agent sockets, credential helpers, and the user's home. Processes use argument arrays, bounded output, timeouts, and process-group termination.

Failed attempts are retained as immutable evidence; attempt identifiers cannot be reused. Cleanup is not automatic. The trusted host alone applies gate-approved patches, creates a bounded commit, and pushes a new task branch. It never switches, stashes, resets, or writes the live checkout.

## Backends and correction loop

`hermes` invokes the verified Council stage launcher, which selects the configured Planner/Coder profile for the deterministic route level and the separate Tester/Reviewer profiles. Worker preflight requires all ten launcher-selected profiles and hashes each profile's `config.yaml`, `SOUL.md`, and `state.db` without recording their contents. Invocation artifacts retain profile/config identities as hashes.

`command` is a future approved-CLI adapter. Every stage is an argument array and the resolved executable is bound to an exact SHA-256. It uses the same Council outboxes and deterministic ingestion/gates. No Codex CLI contract is invented.

`fake` is synthetic test-only evidence. It exercises success, malformed/adversarial output, timeouts, repeated failure, and Coder/Tester/Reviewer correction journeys; it is never represented as a live Council run.

Each correction binds the previous patch, test, and reviewer evidence hashes. A correction always gets a new immutable attempt directory. Policy caps rounds, duration, invocations, changed files, patch bytes, and logs. Repeated failures, path expansion, scientific authority changes, real-data requirements, malformed output, and all limits stop for a human rather than retrying indefinitely.

## Operating commands

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
wpi-council run <task-id> --backend hermes --overnight
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

Every approval binds the current request hash, state hash, base commit, final commit, patch, tests, and reviewer report. Stale or forged hashes fail. Reviewer output can never act as human approval.

The Resource Steward adapter delegates to the existing Phase 4 deterministic CLI. `check`, `show`, `download-candidate`, `verify-candidate`, `build-candidate`, `test-candidate`, `compare`, and `export-report` may run automatically. `promote` and `rollback` always require their own exact human artifact; task or merge approval is insufficient.

## Recovery and troubleshooting

- `doctor` failing on a dirty live checkout: finish or remove local changes yourself; the operator never stashes or resets them.
- Base reported stale: revise the request against the exact current remote commit. Do not mutate a claimed request.
- Claim collision: inspect `status`. Only the remote state-ref winner owns the lease.
- Interrupted process: inspect `status`, preserve the attempt directory, then use `resume` at a supported attempt boundary. Do not reuse a Council run ID.
- Stale lease: keep it visible until an exact human takeover approval or policy-bound expiry is available. Default policy disables automatic takeover.
- Task branch already exists or moved: stop and review it; the operator will not force-push or overwrite it.
- Contract/profile hash mismatch: compare the configured live Council/Hermes installation with the audited contract. Produce and separately approve a compatibility update; never patch the live Council automatically.
- Network failure: no state is inferred from local intent. Re-read the remote state ref and code branch before retrying.
- Handoff conflict: re-read the Obsidian note, calculate its current SHA-256, and explicitly retry `sync-handoff`; synchronization only appends sanitized summaries.

See [`hermes_operator_installation.md`](hermes_operator_installation.md) for the worker bootstrap and acceptance boundary.
