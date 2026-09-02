# Hermes Council operator installation

This package installs only the WPI project-side operator. It does not install
or modify Hermes, Council controls, role profiles, outboxes, model
configuration, credentials, the live Council tree, or the Obsidian reference
snapshot.

The first physical Hermes acceptance attempt began from approved branch
`codex/virtual-lab-finalization-personal` at
`c5688fd36df8061b4bc8975cf9fc3decb5a408e5`, but stopped safely during
operator installation. The selected project virtual environment was valid
Python 3.12.14 and the system `python3` was 3.9.6. Docker, the exact Council
contract, all ten profiles, and the dependency-lock declaration passed before
`python -m pip` failed with `No module named pip`. Worker doctor, Council
roles, model invocation, and task submission were not reached. Physical
acceptance therefore remains incomplete and false; this R2 correction does
not resume it.

## Prerequisites

- macOS `arm64` or `x86_64`;
- Python exactly 3.12 through one of the resolver choices below;
- a running Docker installation;
- the existing live AI Council runtime, not an Obsidian export;
- the existing Hermes executable at a known absolute path;
- Git read/push authentication supplied by the user's credential helper or
  SSH configuration;
- a WPI code remote and, preferably, a separate private control remote.

The audited launcher can select these profiles, all of which must already
exist under `~/.hermes/profiles`: `council-planner-routine`,
`council-planner-standard`, `council-planner-complex`,
`council-planner-critical`, `council-coder-routine`,
`council-coder-standard`, `council-coder-complex`,
`council-coder-critical`, `council-tester`, and `council-reviewer`.

## Exact Python contract

`scripts/resolve-council-python.sh` accepts only Python 3.12 and uses this
deterministic priority:

1. `WPI_COUNCIL_PYTHON`, provided it is an absolute executable path to Python
   3.12;
2. `<project>/.venv/bin/python`, provided it is Python 3.12;
3. an absolute `python3.12` returned by `PATH` lookup;
4. otherwise, stop with the single instruction to install Python 3.12 and run
   `/absolute/path/to/python3.12 -m venv "<project>/.venv"`.

Python 3.9, 3.10, 3.11, and 3.13 are incompatible. The installer does not
alter a system interpreter. If the verified interpreter is the project's
Python 3.12 virtual environment, the editable project installation is placed
there without `--user`. If the verified interpreter is a base Python 3.12,
the installer creates a dedicated environment at
`${WPI_COUNCIL_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/wpi-council}/venv`
and leaves the base environment unchanged.

An accepted existing Python 3.12 virtual environment without recorded operator
state is not required to already contain `pip`. The exact selected interpreter
is passed to `scripts/bootstrap_council_pip.py`, which rechecks Python 3.12 and probes
`python -m pip --version`. Valid pip is preserved without an unnecessary
upgrade. Missing pip is bootstrapped only through that same interpreter's
standard-library `ensurepip --default-pip`, then revalidated. The helper's
bounded result identifies the interpreter, environment prefix, pip version,
module location, distribution root, and whether bootstrap occurred. All pip
locations must be inside the selected environment; system and user-site pip
are rejected. A present but broken or externally resolved pip fails closed and
does not trigger ensurepip.

There is no `get-pip.py`, curl or other downloaded executable bootstrap,
`sudo`, `--user`, shell activation, virtual-environment replacement, or system
Python fallback. If `ensurepip` is unavailable, fails, or does not produce a
pip local to the selected environment, installation fails closed with one
corrective instruction.

## Exact build toolchain

After trusted pip verification and before editable installation, the installer
explicitly installs the build requirement declared by `pyproject.toml` with
the existing constraints authority:

```bash
/absolute/path/to/selected/operator/python -m pip install \
  --constraint constraints-py312.txt \
  setuptools==83.0.0 \
  --requirement requirements-dev.txt
/absolute/path/to/selected/operator/python -m pip install \
  --no-build-isolation \
  --no-deps \
  --editable /absolute/path/to/flyash-phreeqc-ml
```

The installer verifies `setuptools==83.0.0` before the unchanged
`--no-build-isolation --no-deps --editable` operation. Final validation checks
the exact Python version, setuptools, runtime pins, pytest pin, required
imports, editable package path, and `pip check`. Neither ambient setuptools
nor an unconstrained version is accepted.

The install uses the tracked pinned dependency declarations, verifies their
installed versions and required imports, and only then records the exact
interpreter path, Python version, executable SHA-256, and project path in
`operator-python.runtime`. The mode-`0600` record is checked on every later
invocation. The loaded operator also hashes the tracked dependency contract
and reruns the installed-pin validator during doctor, immediately before model
work, and immediately before trusted tests, so later package or declaration
drift fails closed. A valid existing installation is preserved. A mismatched
record, interpreter, environment, launcher, or package import is reported as
an incompatible existing installation and is not overwritten.
The config, installation, and launcher directories must remain beneath the
configured `HOME`; every existing component must be a real, current-user-owned
directory that is not group- or world-writable. Symlinked or shared writable
directory chains are rejected before installation state is written.

## Bootstrap

From the checked-out `flyash-phreeqc-ml` directory on the Hermes computer,
identify the live Council root and, if needed, the exact Python explicitly:

```bash
export WPI_AI_COUNCIL_ROOT='/absolute/path/to/live/AI-Council'
export WPI_COUNCIL_PYTHON='/absolute/path/to/python3.12'
./scripts/install-council-operator.sh
```

Omit `WPI_COUNCIL_PYTHON` when the project's `.venv/bin/python` is already
Python 3.12 and should win the resolver priority. Do not point
`WPI_AI_COUNCIL_ROOT` at the Obsidian `AI Council` directory.

Before writing installation state, the installer checks macOS architecture,
the selected Python, Docker, the exact tracked Council contract hashes,
outboxes, all required profile files, and the dependency lock. It then
completes trusted pip bootstrap, constrained dependency/build-tool
installation, editable installation, installed-pin/import/path validation,
`pip check`, and final Python identity validation. Only after every one of
those gates succeeds may it create a mode-`0600` configuration template at
`~/.config/wpi-virtual-lab/council.toml`, install the resolver, write the
runtime record, or publish the stable launcher. A regular mode-`0600` existing
configuration is preserved byte-for-byte; symlinked, wrong-mode, or otherwise
incompatible configuration is refused.

The stable launcher is installed at
`${WPI_COUNCIL_BIN_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}/wpi-council` and
executes only the Python identity in the runtime record. Put that user-local
bin directory on the normal login-shell `PATH` if it is not already there.
Opening a new terminal and running `wpi-council` never requires manual virtual
environment activation.

Complete the untracked configuration with approved absolute paths and remote
URLs. Do not embed passwords, tokens, keys, or credential-bearing URLs.
Explicitly set `control_remote_private` only after verifying the control
repository's visibility, and set `hermes_executable` to the exact existing
binary.

## Recovery after a stopped installation

The failed R1 attempt can safely leave only directory parents: an empty config
parent, an empty install root, and an existing `~/.local/bin`. The corrected
installer accepts and reuses those real, current-user-owned directories when
they are not group- or world-writable; do not delete them merely to rerun
installation. No config file, runtime record, installed resolver, launcher, or
dedicated operator environment is required for this recoverable shape.

Those empty parents are different from an incompatible partial installation.
Existing config/runtime/resolver/launcher files, symlinks, or a dedicated
environment must satisfy their normal identity, ownership, mode, and content
checks and are never silently replaced. If pip bootstrap, dependency install,
setuptools verification, editable install, installed-pin validation, or final
runtime validation fails, temporary files are removed and no new
`council.toml`, `operator-python.runtime`, installed resolver, internal
launcher, or stable `wpi-council` link is published. A newly created dedicated
operator environment is also removed if it did not reach validated success.

## Worker doctor

After configuration is complete, open a clean login shell and run:

```bash
command -v wpi-council
wpi-council doctor --worker
WPI_AI_COUNCIL_ROOT='/absolute/path/to/live/AI-Council' ./scripts/check-council-operator.sh
```

The worker doctor verifies the tracked policy and clean checkout, the exact
recorded Python 3.12 identity, `setuptools==83.0.0`, all installed dependency
pins, editable checkout identity, `pip check`, the Council hashes and profiles,
Docker, and the configured Hermes executable. For both remotes it verifies
read access and
performs a Git push dry run to a fresh nonexistent ref in the namespace the
operator will use. It checks that the probe ref is absent before and after, so
the test creates no branch or state ref. The result separates readability,
dry-run push authorization, authentication failure, network failure, and
branch/ref-policy refusal. Read access alone is not sufficient, and no model
stage can start unless both code-branch and control-state write preflights pass.

A green doctor establishes only `worker doctor passed`. It does not establish
that a live Planner/Coder/Tester/Reviewer task passed, that a task branch was
approved, or that any merge or deployment occurred.

## Foreground and background execution

After the controller submits a task, a foreground run is:

```bash
export WPI_TASK_ID='<submitted-task-id>'
./scripts/run-hermes-task.sh "$WPI_TASK_ID"
```

The script resolves the recorded Python, runs worker doctor, claims through
the control remote, and executes the bounded Hermes pipeline as an attended
foreground process. It does not claim an overnight sleep assertion. For safe
supervised background or overnight operation:

```bash
./scripts/start-hermes-worker.sh "$WPI_TASK_ID"
./scripts/stop-hermes-worker.sh
```

The shell entry points always use the recorded interpreter. The Python
supervisor binds one task to one worker-instance ID and stores atomic,
mode-`0600` metadata under
`${XDG_STATE_HOME:-$HOME/.local/state}/wpi-council`. It retains the requested
and effective command identity, exact Python identity, executable hashes,
kernel process-start identity, process group/session identity, and the stop
capability. It refuses malformed metadata, legacy PID-only state, PID reuse,
and unrelated processes.

The worker runs in its own process group/session. On macOS the supervisor uses
the verified absolute `/usr/bin/caffeinate -dimsu`, making sleep prevention an
explicit part of the command identity; the sleep assertion ends with the
worker. Stop requests go through the owning supervisor, which rechecks
identity, sends SIGTERM to the complete owned process group, waits a bounded
period, uses SIGKILL only if the same owned group remains, and reaps the child.
Active metadata is removed only after verified shutdown. An already completed
worker is reported honestly, while the remote task state remains authoritative
for evidence and lease recovery.

Use `wpi-council status <task-id>` from either computer to inspect progress.
Do not delete or edit lifecycle metadata to force a stop.

## Acceptance boundary

The repository's deterministic temporary-environment, local-bare-remote,
worker-lifecycle, stale-approval replay, and two-clone journeys are simulation
evidence. They are not a live Hermes acceptance claim.

Follow [`hermes_physical_acceptance.md`](hermes_physical_acceptance.md) only
when a human explicitly resumes physical acceptance. The first attempt stopped
inside installation before worker doctor or model/task execution. The runbook
separately records `worker doctor passed`, `live four-role task passed`, and
`task branch approved`; none implies merge or deployment. Phase 5-R2 work is
performed only on the Personal computer and leaves live Hermes, the live
Council, and the Obsidian `AI Council` snapshot untouched.
