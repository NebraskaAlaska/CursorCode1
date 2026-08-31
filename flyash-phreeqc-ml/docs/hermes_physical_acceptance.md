# Physical Hermes acceptance runbook

Status: **not run**. Phase 5-R1 changed and tested the repository-side
installation and lifecycle contracts only. Use this runbook later, from the
physical Hermes and Personal computers, only after a human explicitly starts
live acceptance.

This is a documentation-only acceptance. It must not change the live Council,
`.obsidian`, scientific code or data, repository visibility, branch
protection, `main`, deployment state, or resource-promotion state. Do not save
credentials, raw role transcripts, user data, databases, model weights, or
runtime workspaces in Git or the acceptance evidence.

## 1. Pin the approved checkout on Hermes

Locate the existing Hermes checkout; do not create a second checkout as an
installation shortcut. Set these values without hard-coding a username:

```bash
export WPI_CHECKOUT='/absolute/path/to/the/current/VirtualLAB-Codex'
export WPI_PROJECT="$WPI_CHECKOUT/flyash-phreeqc-ml"
export WPI_APPROVED_BRANCH='codex/virtual-lab-finalization-personal'
export WPI_APPROVED_SHA='<exact Phase 5-R1 SHA from the final handoff>'
export WPI_REMOTE_REF="refs/remotes/origin/$WPI_APPROVED_BRANCH"
cd "$WPI_CHECKOUT"
```

Require the expected repository, branch, and a completely clean checkout,
including no untracked file. Fetch only the approved branch and require the
local commit, remote commit, and divergence to match exactly:

```bash
test "$(git branch --show-current)" = "$WPI_APPROVED_BRANCH"
test -z "$(git status --porcelain --untracked-files=all)"
git fetch origin "refs/heads/$WPI_APPROVED_BRANCH:refs/remotes/origin/$WPI_APPROVED_BRANCH"
test "$(git rev-parse HEAD)" = "$WPI_APPROVED_SHA"
test "$(git rev-parse "$WPI_REMOTE_REF")" = "$WPI_APPROVED_SHA"
test "$(git rev-list --left-right --count "HEAD...$WPI_REMOTE_REF" | tr '\t' ' ')" = '0 0'
```

If any assertion fails, stop. This runbook does not authorize switching,
merging, rebasing, resetting, or cleaning a checkout to make it pass.

## 2. Identify live Council, Hermes, and Python

Set the existing authoritative runtime locations explicitly. The Council root
must be the live installation containing the required controls, not the
Obsidian `AI Council` reference directory.

```bash
export WPI_AI_COUNCIL_ROOT='/absolute/path/to/live/AI-Council'
export WPI_HERMES_EXECUTABLE='/absolute/path/to/the/existing/hermes'
test -d "$WPI_AI_COUNCIL_ROOT"
test -f "$WPI_AI_COUNCIL_ROOT/tools/council_route.py"
test -f "$WPI_AI_COUNCIL_ROOT/tools/council_stage_launcher.py"
test -x "$WPI_HERMES_EXECUTABLE"
```

Use the project's Python 3.12 virtual environment when it is the approved
runtime:

```bash
export WPI_COUNCIL_PYTHON="$WPI_PROJECT/.venv/bin/python"
test -x "$WPI_COUNCIL_PYTHON"
test "$("$WPI_COUNCIL_PYTHON" -c 'import sys; print("3.12" if sys.version_info[:2] == (3, 12) else "wrong")')" = '3.12'
```

If the project does not have that environment, set `WPI_COUNCIL_PYTHON` to an
existing absolute Python 3.12 executable. Do not use or alter system Python
3.9, and do not activate an environment manually.

## 3. Install without touching Council

From the approved project checkout, run the corrected installer:

```bash
cd "$WPI_PROJECT"
./scripts/install-council-operator.sh
```

Record the installer's reported interpreter path, 3.12 version, executable
SHA-256, and stable launcher path. Verify the untracked configuration and
runtime record permissions:

```bash
export WPI_COUNCIL_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/wpi-virtual-lab/council.toml"
export WPI_RUNTIME_RECORD="${WPI_COUNCIL_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/wpi-council}/operator-python.runtime"
test "$(stat -f '%Lp' "$WPI_COUNCIL_CONFIG")" = '600'
test "$(stat -f '%Lp' "$WPI_RUNTIME_RECORD")" = '600'
```

Complete `council.toml` with the current checkout, code remote, separately
verified control remote and privacy attestation, stable worker ID, disposable
sandbox/state-cache paths, live Council root, and exact Hermes executable. Do
not put a password, key, token, cookie, or credential-bearing URL in it. Do not
track the file.

Open a new login terminal without activating any environment, then require the
stable launcher and recorded interpreter:

```bash
export WPI_PROJECT='/absolute/path/to/the/current/VirtualLAB-Codex/flyash-phreeqc-ml'
export WPI_RUNTIME_RECORD="${WPI_COUNCIL_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/wpi-council}/operator-python.runtime"
command -v wpi-council
wpi-council --help
sed -n '1,4p' "$WPI_RUNTIME_RECORD"
```

## 4. Establish only the worker-doctor checkpoint

Run the corrected doctor from the new terminal:

```bash
wpi-council doctor --worker
cd "$WPI_PROJECT"
./scripts/check-council-operator.sh
```

Inspect the bounded JSON and require all of the following before continuing:

- `operator_python.version` is exactly `3.12.*`, with the intended absolute
  interpreter path and recorded SHA-256;
- `code_remote_readable` and `code_remote_push_authorized` are `true`;
- `control_remote_readable` and `control_remote_push_authorized` are `true`;
- `remote_permission_preflight_non_mutating` is `true`;
- Council/profile identities, Docker, and the exact Hermes executable pass;
- no permission-preflight ref exists on either remote after the command.

At this point record only:

```text
worker doctor passed = true
live four-role task passed = false
task branch approved = false
merge performed = false
deployment performed = false
```

## 5. Submit one sanitized documentation task from Personal

On the Personal computer, require its approved checkout and operator doctor in
the same way. Choose one previously unused public task ID and create exactly a
documentation-only request:

```bash
export WPI_CHECKOUT='/absolute/path/to/the/current/VirtualLAB-Codex'
export WPI_PROJECT="$WPI_CHECKOUT/flyash-phreeqc-ml"
export WPI_APPROVED_BRANCH='codex/virtual-lab-finalization-personal'
export WPI_APPROVED_SHA='<exact Phase 5-R1 SHA from the final handoff>'
export WPI_RUNTIME_RECORD="${WPI_COUNCIL_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/wpi-council}/operator-python.runtime"
export WPI_TASK_ID='phase5-r1-hermes-docs-acceptance-01'
cd "$WPI_PROJECT"
wpi-council doctor
wpi-council create-request "/tmp/$WPI_TASK_ID.md" \
  --task-id "$WPI_TASK_ID" \
  --title 'Add the sanitized Hermes acceptance sentinel' \
  --goal 'Create docs/hermes_acceptance_sentinel.md with one short statement that this task is a documentation-only operator-path check and makes no scientific, validation, merge, or deployment claim.' \
  --background 'Public sanitized Phase 5 physical acceptance; no user data, credentials, scientific data, or live Council content.' \
  --allowed-path 'docs/hermes_acceptance_sentinel.md' \
  --acceptance 'Only the requested documentation sentinel and trusted council-results manifest change.' \
  --acceptance 'All immutable trusted tests pass after the Council tester gate.' \
  --documentation 'docs/council_operator.md' \
  --scientific-risk moderate \
  --security-privacy sanitized \
  --requester 'human:maintainer' \
  --backend hermes
wpi-council submit "/tmp/$WPI_TASK_ID.md"
wpi-council status "$WPI_TASK_ID"
```

If that task ID already exists, stop and create a newly reviewed request with a
new ID; do not alter or overwrite an earlier state ref.

## 6. Claim and execute on Hermes

Back on Hermes, use the safe background supervisor so the live acceptance also
exercises ownership metadata, process-group handling, and macOS sleep
prevention:

```bash
export WPI_TASK_ID='phase5-r1-hermes-docs-acceptance-01'
cd "$WPI_PROJECT"
./scripts/start-hermes-worker.sh "$WPI_TASK_ID"
```

The owned worker runs doctor, claims the exact submitted request through the
control remote, and runs it with `--backend hermes --overnight`. Do not edit a
PID or metadata file, manually invoke `kill`, or modify the live Council while
it runs. If a deliberate interruption test is separately approved, use only:

```bash
./scripts/stop-hermes-worker.sh
```

After interruption, inspect the remote task state and use the documented
expiry/exact-approval/resume path. Never infer lease ownership from a local
file alone.

## 7. Monitor and verify from Personal

From Personal:

```bash
wpi-council watch "$WPI_TASK_ID" --max-seconds 28800
wpi-council review "$WPI_TASK_ID" --output "/tmp/$WPI_TASK_ID-review.json"
wpi-council status "$WPI_TASK_ID" > "/tmp/$WPI_TASK_ID-status.json"
```

Use the recorded operator Python to verify the terminal state and the four
bounded invocation identities without reading raw role output:

```bash
export WPI_OPERATOR_PYTHON="$(sed -n '2p' "$WPI_RUNTIME_RECORD")"
"$WPI_OPERATOR_PYTHON" -c 'import json, sys
record=json.load(open(sys.argv[1], encoding="utf-8"))
assert record["state"] == "awaiting_human_review"
profiles=record["evidence"]["summary"]["role_profiles"]
assert set(profiles) == {"planner", "coder", "tester", "reviewer"}
assert all(isinstance(value, str) and len(value) == 64 for value in profiles.values())
assert record["evidence"]["summary"]["reviewer_verdict"] == "APPROVE"' "/tmp/$WPI_TASK_ID-status.json"
```

Verify the ordinary task branch exists at the exact final commit:

```bash
export WPI_TASK_BRANCH="council/wpi/$WPI_TASK_ID"
export WPI_TASK_COMMIT="$("$WPI_OPERATOR_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["final_task_commit"])' "/tmp/$WPI_TASK_ID-status.json")"
test "$(git ls-remote --heads origin "refs/heads/$WPI_TASK_BRANCH" | cut -f1)" = "$WPI_TASK_COMMIT"
git fetch origin "refs/heads/$WPI_TASK_BRANCH:refs/remotes/origin/$WPI_TASK_BRANCH"
git diff --check "$WPI_APPROVED_SHA..refs/remotes/origin/$WPI_TASK_BRANCH"
git diff --stat "$WPI_APPROVED_SHA..refs/remotes/origin/$WPI_TASK_BRANCH"
git diff "$WPI_APPROVED_SHA..refs/remotes/origin/$WPI_TASK_BRANCH" -- \
  docs/hermes_acceptance_sentinel.md "council-results/$WPI_TASK_ID.json"
```

Inspect the complete bounded review manifest, the actual diff, requested and
effective trusted-test arguments, exact trusted-test Python identity, Council
gate hashes, and Reviewer verdict. Only after those checks may the human record:

```text
worker doctor passed = true
live four-role task passed = true
task branch approved = false
merge performed = false
deployment performed = false
```

On Hermes, call the safe stop entry point after normal completion and require
an honest `already_finished` result; it must not signal another process. Then
release the completed remote lease through the pinned operator instead of
editing local metadata or remote state directly:

```bash
cd "$WPI_PROJECT"
./scripts/stop-hermes-worker.sh
wpi-council release "$WPI_TASK_ID"
```

## 8. Decide only the task branch

Choose exactly one command after human review:

```bash
wpi-council approve-task-branch "$WPI_TASK_ID" --approved-by 'human:<name>'
```

or:

```bash
wpi-council reject "$WPI_TASK_ID" --actor 'human:<name>' --reason '<bounded reviewed reason>'
```

Do not call pull-request, merge, deployment, resource-promotion, or rollback
approval commands. Do not merge or deploy the task branch. If approved, record
`task branch approved = true`; if rejected, record it as `false`. In both cases
`merge performed = false` and `deployment performed = false` remain mandatory.

## 9. Append the Obsidian handoff

Resolve the one existing `Obsidian (AI Testing)` vault that contains
`.obsidian`, `AI Council`, and `WPI Project`. Set its absolute path without a
username-specific example, and verify the target before writing:

```bash
export WPI_OBSIDIAN_VAULT='/absolute/path/to/the/existing/Obsidian (AI Testing)'
export WPI_HANDOFF_NOTE="$WPI_OBSIDIAN_VAULT/WPI Project/99 - Handoff Log.md"
test -d "$WPI_OBSIDIAN_VAULT/.obsidian"
test -d "$WPI_OBSIDIAN_VAULT/AI Council"
test -d "$WPI_OBSIDIAN_VAULT/WPI Project"
test -f "$WPI_HANDOFF_NOTE"
export WPI_HANDOFF_BEFORE_SHA="$(shasum -a 256 "$WPI_HANDOFF_NOTE" | cut -d ' ' -f1)"
wpi-council sync-handoff "$WPI_TASK_ID" --expected-before-hash "$WPI_HANDOFF_BEFORE_SHA"
```

The sync must append a sanitized bounded entry to the handoff note only. Verify
that `.obsidian` and `AI Council` were not modified, and retain the exact before
and after handoff-note hashes. Do not copy Obsidian or live Council files into
the repository or review bundle.

## Final acceptance record

Keep these decisions as separate facts:

- **worker doctor passed**: both remote reads and non-mutating namespace push
  preflights, exact Python/Council/Hermes identities, profiles, and Docker
  passed;
- **live four-role task passed**: Planner, Coder, Tester, Reviewer, Council
  gates, pinned trusted tests, normal task-branch push, and
  `awaiting_human_review` were observed on physical Hermes;
- **task branch approved**: a human approved only the exact reviewed task
  branch, or leave this false when rejected;
- **merge/deployment not performed**: no merge, deployment, PR, scientific
  validation, or resource promotion occurred.

Until every applicable check above is performed and recorded from the physical
machines, live Hermes acceptance remains false.
