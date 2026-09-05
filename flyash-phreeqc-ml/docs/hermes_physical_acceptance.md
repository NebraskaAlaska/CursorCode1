# Physical Hermes acceptance runbook

Status: **R2 physical task failed immutably; R3 physical acceptance has not
been run**.

Phase 5-R2 project-side CI was green at
`1fa588ac4cdd3a67e293ca219cd5b96e0c783aee` (run `33626721683`). The later
physical task `phase5-r2-hermes-docs-acceptance-01` is an immutable failed
audit record. Do not alter, delete, resubmit, or reinterpret it.

## Sanitized R2 physical evidence

Worker doctor passed. The physical Planner then ran twice, producing
substantial `SPEC`, `FILE_PLAN`, `ACCEPTANCE_CRITERIA`, `EDGE_CASES`,
`CHANGE_POLICY`, and `ROUTE_RECOMMENDATION` artifacts in these immutable
attempts:

- `wpi-phase5-r2-hermes-docs-acceptance-01-attempt-00`;
- `wpi-phase5-r2-hermes-docs-acceptance-01-attempt-01`.

Both attempts used Planner `claude-opus-5` at effort `max`; the configured
Coder was `claude-opus-5`, Tester `gpt-5.6-sol`, and Reviewer
`gpt-5.6-sol`. The Planner deliberately returned `{"status":"BLOCKED"}` in
both attempts. Each operator result was therefore
`correction_kind=planner_blocked` and
`correction_detail=Planner did not produce a READY plan`. The trusted
operator stopped when the same automatic failure repeated. Coder, Tester,
and Reviewer were never invoked.

The supervisor ended normally with exit code `0`, reason
`normal_completion`, and `child_reaped=true`. No task branch was pushed and
no merge or publication action followed. This bounded record contains no raw
role transcript or private control-repository state.

The Planner correctly identified project-side integration defects:

- the operator workspace is the Git root `VirtualLAB-Codex/`, while the
  application is under `flyash-phreeqc-ml/`; the R2 request path and later
  app-root diff path therefore named different files;
- the Git-root trusted compile command found none of its six app-root targets,
  printed six `Can't list` lines, compiled zero files, and nevertheless exited
  `0`;
- a one-path documentation allowlist left the mandatory Tester no safe
  non-empty output path, while `council-results/**` is trusted-host space;
- the request text contained the prohibited substring `deploy`, which the
  existing fail-closed request policy correctly rejects even in a negative
  phrase;
- the operator looked for `scripts/release_scan.py` at the Git root even
  though the required scanner is nested under `flyash-phreeqc-ml/`;
- forbidden paths were partly expressed as if the application were the Git
  root; and
- the router received the hard cap `max_changed_files` as though it were an
  estimated change count.

Phase 5-R3 corrects only these project-side contracts. It does not change the
live Council, role profiles, Council prompts, private control state, or the
failed R2 task. It does not begin the product phase or a scientific phase.

## Authoritative path and ownership contract

`WPI_CHECKOUT` is the authoritative Git workspace root and must contain
`.git`. `WPI_PROJECT="$WPI_CHECKOUT/flyash-phreeqc-ml"` is the application
subtree, not a second repository root.

All request `allowed_paths` and `forbidden_paths`, every Coder or Tester patch,
and every human diff path are Git-root-relative. Immutable trusted commands
run with `cwd=$WPI_CHECKOUT`. The trusted host alone generates
`council-results/<task-id>.json` after automatic gates. No Planner, Coder,
Tester, or Reviewer may write `council-results/**`.

The new task provides two role-writable paths: one documentation path for
Coder and one deterministic test path for Tester. The trusted result manifest
is the separately allowed host-generated `+1` path.

## Computer-specific topology

### PERSONAL COMPUTER

`WPI_CHECKOUT` is the Git root and `WPI_PROJECT` is its nested
`flyash-phreeqc-ml/` application subtree. Use the existing
`$WPI_PROJECT/.venv/bin/python` and the existing mode-`0600`, XDG-aware
`council-personal.toml`. Personal invokes the tracked CLI module explicitly
from `WPI_PROJECT`; it does not require an installed `wpi-council` launcher,
a live Council root, or a Hermes executable. Ordinary Personal doctor must
report `worker_ready=false` before request creation.

### HERMES COMPUTER

The established stable installed `wpi-council` launcher remains the Hermes
entry point. Its worker configuration is the XDG-aware `council.toml`; the
live Council root and exact Hermes executable are required, and worker doctor
must report `worker_ready=true`. Do not copy the Hermes configuration shape to
Personal or assume that the two computers have identical installation
topology.

## HERMES COMPUTER — 1. Pin the approved checkout

Locate the existing Hermes checkout. Do not create another checkout as an
installation shortcut and do not switch, reset, clean, merge, or rebase it to
make these assertions pass:

```bash
export WPI_CHECKOUT='/absolute/path/to/the/current/VirtualLAB-Codex'
export WPI_PROJECT="$WPI_CHECKOUT/flyash-phreeqc-ml"
export WPI_APPROVED_BRANCH='codex/virtual-lab-finalization-personal'
export WPI_APPROVED_SHA='<exact Phase 5-R3-R1 SHA from the final handoff>'
export WPI_REMOTE_REF="refs/remotes/origin/$WPI_APPROVED_BRANCH"
cd "$WPI_CHECKOUT"
test -d .git
test -d flyash-phreeqc-ml
test "$(git branch --show-current)" = "$WPI_APPROVED_BRANCH"
test -z "$(git status --porcelain --untracked-files=all)"
git fetch origin "refs/heads/$WPI_APPROVED_BRANCH:refs/remotes/origin/$WPI_APPROVED_BRANCH"
test "$(git rev-parse HEAD)" = "$WPI_APPROVED_SHA"
test "$(git rev-parse "$WPI_REMOTE_REF")" = "$WPI_APPROVED_SHA"
test "$(git rev-list --left-right --count "HEAD...$WPI_REMOTE_REF" | tr '\t' ' ')" = '0 0'
```

If any assertion fails, stop.

## HERMES COMPUTER — 2. Identify the existing runtime without changing Council

The Council root must be the live installation, never the Obsidian reference
directory:

```bash
export WPI_AI_COUNCIL_ROOT='/absolute/path/to/live/AI-Council'
export WPI_HERMES_EXECUTABLE='/absolute/path/to/the/existing/hermes'
export WPI_COUNCIL_PYTHON="$WPI_PROJECT/.venv/bin/python"
test -d "$WPI_AI_COUNCIL_ROOT"
test -f "$WPI_AI_COUNCIL_ROOT/tools/council_route.py"
test -f "$WPI_AI_COUNCIL_ROOT/tools/council_stage_launcher.py"
test -x "$WPI_HERMES_EXECUTABLE"
test -x "$WPI_COUNCIL_PYTHON"
test "$("$WPI_COUNCIL_PYTHON" -c 'import sys; print("3.12" if sys.version_info[:2] == (3, 12) else "wrong")')" = '3.12'
```

If the project environment is not the approved Python 3.12 runtime, point
`WPI_COUNCIL_PYTHON` at an existing absolute Python 3.12 executable. Do not
alter system Python, activate another environment, replace a venv, or use
downloaded bootstrap content.

## HERMES COMPUTER — 3. Install or verify the R3 project-side operator

From the application subtree:

```bash
cd "$WPI_PROJECT"
./scripts/install-council-operator.sh
```

The installer uses only the selected Python 3.12. A genuinely pipless selected
environment may use that interpreter's local standard-library `ensurepip`.
The installer preserves `setuptools==83.0.0`, exact dependency identities,
`--no-build-isolation --no-deps`, editable-package identity, and `pip check`.
It publishes no config, runtime record, resolver, or launcher after failed
validation.

Complete the untracked operator config with `repository_path=$WPI_CHECKOUT`,
not `$WPI_PROJECT`. Keep the existing code/control remotes, privacy
attestation, sandbox/state roots, live Council root, exact Hermes executable,
and worker identity. Do not put authentication material in the file and do
not track it.

Verify the recorded runtime from a fresh login shell without manual
activation:

```bash
export WPI_COUNCIL_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/wpi-virtual-lab/council.toml"
export WPI_RUNTIME_RECORD="${WPI_COUNCIL_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/wpi-council}/operator-python.runtime"
test "$(stat -f '%Lp' "$WPI_COUNCIL_CONFIG")" = '600'
test "$(stat -f '%Lp' "$WPI_RUNTIME_RECORD")" = '600'
export WPI_OPERATOR_PYTHON="$(sed -n '2p' "$WPI_RUNTIME_RECORD")"
test -x "$WPI_OPERATOR_PYTHON"
"$WPI_OPERATOR_PYTHON" -m pip --version
"$WPI_OPERATOR_PYTHON" "$WPI_PROJECT/scripts/validate_dependency_lock.py" --installed
"$WPI_OPERATOR_PYTHON" -m pip check
command -v wpi-council
wpi-council --help
```

## HERMES COMPUTER — 4. Establish only the worker-doctor checkpoint

```bash
wpi-council doctor --worker
cd "$WPI_PROJECT"
./scripts/check-council-operator.sh
```

Require the exact Python, dependencies, Council hashes, ten role profiles,
Docker, Hermes executable, code/control reads, and non-mutating namespace
push preflights to pass. Require no probe ref to remain. At this point record
only:

```text
worker doctor passed = true
live four-role task passed = false
task branch approved = false
merge performed = false
publication action performed = false
```

## PERSONAL COMPUTER — 5. Create and submit the new immutable request

Use the existing Personal checkout, project Python, and controller config.
These checks are fail-closed: a missing config, any mode other than `0600`, a
missing project Python, a Python other than 3.12, or a configured
`repository_path` that resolves anywhere except `WPI_CHECKOUT` stops the
runbook before doctor or request creation. Do not install another launcher,
create another config, alter the existing config, or fall back to
`council.toml`.

<!-- R3_PERSONAL_PREFLIGHT_BEGIN -->
```bash
export WPI_CHECKOUT='/absolute/path/to/the/current/VirtualLAB-Codex'
export WPI_PROJECT="$WPI_CHECKOUT/flyash-phreeqc-ml"
export WPI_APPROVED_BRANCH='codex/virtual-lab-finalization-personal'
export WPI_APPROVED_SHA='<exact Phase 5-R3-R1 SHA from the final handoff>'
export WPI_REMOTE_REF="refs/remotes/origin/$WPI_APPROVED_BRANCH"
export WPI_PERSONAL_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/wpi-virtual-lab/council-personal.toml"
export WPI_PERSONAL_PYTHON="$WPI_PROJECT/.venv/bin/python"
export WPI_TASK_ID='phase5-r3-hermes-docs-acceptance-01'

test "$(git -C "$WPI_CHECKOUT" rev-parse --show-toplevel)" = "$(cd "$WPI_CHECKOUT" && pwd -P)"
test -d "$WPI_PROJECT"
test "$(git -C "$WPI_CHECKOUT" branch --show-current)" = "$WPI_APPROVED_BRANCH"
test -z "$(git -C "$WPI_CHECKOUT" status --porcelain --untracked-files=all)"
git -C "$WPI_CHECKOUT" fetch origin "refs/heads/$WPI_APPROVED_BRANCH:refs/remotes/origin/$WPI_APPROVED_BRANCH"
test "$(git -C "$WPI_CHECKOUT" rev-parse HEAD)" = "$WPI_APPROVED_SHA"
test "$(git -C "$WPI_CHECKOUT" rev-parse "$WPI_REMOTE_REF")" = "$WPI_APPROVED_SHA"
test "$(git -C "$WPI_CHECKOUT" rev-list --left-right --count "HEAD...$WPI_REMOTE_REF" | tr '\t' ' ')" = '0 0'
test -f "$WPI_PERSONAL_CONFIG"
test ! -L "$WPI_PERSONAL_CONFIG"
if stat -f '%Lp' "$WPI_PERSONAL_CONFIG" >/dev/null 2>&1; then
  WPI_PERSONAL_CONFIG_MODE="$(stat -f '%Lp' "$WPI_PERSONAL_CONFIG")"
else
  WPI_PERSONAL_CONFIG_MODE="$(stat -c '%a' "$WPI_PERSONAL_CONFIG")"
fi
test "$WPI_PERSONAL_CONFIG_MODE" = '600'
unset WPI_PERSONAL_CONFIG_MODE
test -x "$WPI_PERSONAL_PYTHON"
test "$("$WPI_PERSONAL_PYTHON" -c 'import sys; print("3.12" if sys.version_info[:2] == (3, 12) else "wrong")')" = '3.12'
"$WPI_PERSONAL_PYTHON" -c 'import sys, tomllib; from pathlib import Path; config = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); actual = Path(config["operator"]["repository_path"]).expanduser().resolve(); expected = Path(sys.argv[2]).resolve(); raise SystemExit(0 if actual == expected else "Personal repository_path must resolve to WPI_CHECKOUT")' "$WPI_PERSONAL_CONFIG" "$WPI_CHECKOUT"

council_personal() {
  (
    cd "$WPI_PROJECT"
    "$WPI_PERSONAL_PYTHON" \
      -m flyash_phreeqc_ml.council_operator.cli \
      --config "$WPI_PERSONAL_CONFIG" \
      "$@"
  )
}
```
<!-- R3_PERSONAL_PREFLIGHT_END -->

Run the ordinary Personal doctor and require the saved JSON to report
`worker_ready=false`, successful code/control reads and dry-run push
authorization, and a non-mutating permission preflight before creating the
request:

```bash
council_personal doctor > "/tmp/$WPI_TASK_ID-personal-doctor.json"
"$WPI_PERSONAL_PYTHON" -c 'import json, sys; report = json.load(open(sys.argv[1], encoding="utf-8")); assert report["worker_ready"] is False; assert report["code_remote_readable"] is True; assert report["code_remote_push_authorized"] is True; assert report["control_remote_readable"] is True; assert report["control_remote_push_authorized"] is True; assert report["remote_permission_preflight_non_mutating"] is True' "/tmp/$WPI_TASK_ID-personal-doctor.json"
```

The following executable example is covered by the R3 policy-validation test.
Its two `--test-command` values are byte-for-byte equivalent to the tracked
defaults and execute from the Git root.

<!-- R3_CREATE_REQUEST_BEGIN -->
```bash
council_personal create-request "/tmp/$WPI_TASK_ID.md" \
  --task-id "$WPI_TASK_ID" \
  --title 'Add the sanitized Hermes acceptance sentinel and test' \
  --goal 'Create the approved documentation sentinel and its deterministic acceptance test at the two explicitly allowed repository paths.' \
  --background 'Sanitized public physical Council-path acceptance using no scientific content.' \
  --allowed-path 'flyash-phreeqc-ml/docs/hermes_acceptance_sentinel.md' \
  --allowed-path 'flyash-phreeqc-ml/tests/test_hermes_acceptance_sentinel.py' \
  --acceptance 'Coder changes only the approved documentation path.' \
  --acceptance 'Tester changes only the approved test path.' \
  --acceptance 'The sentinel contains exactly: This is a documentation-only Council operator-path acceptance sentinel.' \
  --acceptance 'The deterministic test verifies that exact sentinel content.' \
  --acceptance 'All corrected immutable trusted tests pass.' \
  --acceptance 'The trusted host may separately generate its standard council-results manifest; no model role writes that namespace.' \
  --test-command 'python3 -m compileall -q flyash-phreeqc-ml/app.py flyash-phreeqc-ml/app_ui.py flyash-phreeqc-ml/flyash_phreeqc_ml flyash-phreeqc-ml/scripts flyash-phreeqc-ml/ui flyash-phreeqc-ml/tests' \
  --test-command 'python3 -m pytest -q -p no:cacheprovider flyash-phreeqc-ml/tests' \
  --documentation 'flyash-phreeqc-ml/docs/council_operator.md' \
  --scientific-risk moderate \
  --security-privacy sanitized \
  --requester 'human:maintainer' \
  --backend hermes \
  --max-changed-files 2
```
<!-- R3_CREATE_REQUEST_END -->

Then submit and inspect only the new request:

```bash
council_personal submit "/tmp/$WPI_TASK_ID.md"
council_personal status "$WPI_TASK_ID"
```

If this task ID already exists, stop and prepare a separately reviewed new ID.
Never overwrite an existing state ref, and do not reuse or edit the failed R2
task.

## HERMES COMPUTER — 6. Claim and execute only after separate physical authorization

This document describes the future operator steps; Phase 5-R3 repository work
does not authorize running them. On Hermes, after an explicit human resumption:

```bash
export WPI_TASK_ID='phase5-r3-hermes-docs-acceptance-01'
cd "$WPI_PROJECT"
./scripts/start-hermes-worker.sh "$WPI_TASK_ID"
```

The owned supervisor runs doctor, claims through the remote state authority,
and invokes the unchanged Planner, Coder, Tester, and Reviewer sequence. Do not
edit worker metadata, signal a worker manually, change the live Council, or
write model output into `council-results/**`.

## PERSONAL COMPUTER — 7. Monitor and verify from the Git root

From Personal:

```bash
council_personal watch "$WPI_TASK_ID" --max-seconds 28800
council_personal review "$WPI_TASK_ID" --output "/tmp/$WPI_TASK_ID-review.json"
council_personal status "$WPI_TASK_ID" > "/tmp/$WPI_TASK_ID-status.json"
```

Use the explicit Personal project Python to require `awaiting_human_review`,
four bounded invocation identities, and an approved Reviewer verdict. This is
model and operator evidence, not scientific validation.

Verify the task branch and inspect the exact Git-root-relative paths from
`$WPI_CHECKOUT`, never from `$WPI_PROJECT`:

```bash
export WPI_TASK_BRANCH="council/wpi/$WPI_TASK_ID"
export WPI_TASK_COMMIT="$("$WPI_PERSONAL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["final_task_commit"])' "/tmp/$WPI_TASK_ID-status.json")"
cd "$WPI_CHECKOUT"
test "$(git ls-remote --heads origin "refs/heads/$WPI_TASK_BRANCH" | cut -f1)" = "$WPI_TASK_COMMIT"
git fetch origin "refs/heads/$WPI_TASK_BRANCH:refs/remotes/origin/$WPI_TASK_BRANCH"
git diff --check "$WPI_APPROVED_SHA..refs/remotes/origin/$WPI_TASK_BRANCH"
git diff --stat "$WPI_APPROVED_SHA..refs/remotes/origin/$WPI_TASK_BRANCH"
git diff "$WPI_APPROVED_SHA..refs/remotes/origin/$WPI_TASK_BRANCH" -- \
  flyash-phreeqc-ml/docs/hermes_acceptance_sentinel.md \
  flyash-phreeqc-ml/tests/test_hermes_acceptance_sentinel.py \
  "council-results/$WPI_TASK_ID.json"
```

The sentinel must contain only the statement that it is a documentation-only
Council operator-path acceptance sentinel. The deterministic test must verify
that narrow content contract and contain no private, live, or scientific data.
Reject any third role-authored path and any role-authored
`council-results/**` path.

## HERMES COMPUTER — Release the completed worker lease

Use the safe stop entry point after normal completion and release the completed
lease through the established stable launcher:

```bash
cd "$WPI_PROJECT"
./scripts/stop-hermes-worker.sh
wpi-council release "$WPI_TASK_ID"
```

## PERSONAL COMPUTER — 8. Decide only the task branch

After human review, choose exactly one:

```bash
council_personal approve-task-branch "$WPI_TASK_ID" --approved-by 'human:<name>'
```

or:

```bash
council_personal reject "$WPI_TASK_ID" --actor 'human:<name>' --reason '<bounded reviewed reason>'
```

Do not issue any other approval or publication command. Task-branch approval
does not modify the base branch.

## 9. Append the physical handoff only after a real run

Resolve the existing `Obsidian (AI Testing)` vault that contains `.obsidian`,
`AI Council`, and `WPI Project`. Append only a sanitized bounded physical
result to `WPI Project/99 - Handoff Log.md`, with before/after hashes. Do not
modify `.obsidian` or `AI Council`, and do not copy vault or live Council
content into Git.

## Current final acceptance record

- R2 project-side CI was green.
- The physical R2 Planner task failed twice with the same deliberate BLOCKED
  result and remains immutable.
- Phase 5-R3 corrects the project-side Git-root integration contracts.
- Phase 5-R3-R1 corrects the Personal runbook to use the existing explicit
  project Python and `council-personal.toml`; it does not alter the R3 request.
- Live R3 Planner/Coder/Tester/Reviewer acceptance has **not** run.
- No R3 task has been submitted and no R3 model has been invoked.
- No task branch has been approved, merged, or published by this work.

Until every future physical check is actually performed and recorded, live
Hermes acceptance and dual-computer acceptance remain false.
