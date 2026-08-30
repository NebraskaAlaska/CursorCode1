# Hermes Council operator installation

This package installs only the WPI project-side operator. It does not install or modify Hermes, Council controls, role profiles, outboxes, model configuration, credentials, or the Obsidian reference snapshot.

## Prerequisites

- macOS `arm64` or `x86_64`
- Python exactly 3.12
- a running Docker installation
- the existing live AI Council runtime (not an Obsidian export)
- the existing Hermes executable
- Git read/push authentication supplied by the user's credential helper or SSH configuration
- a WPI code remote and, preferably, a separate private control remote

The audited launcher can select these profiles, all of which must already exist under `~/.hermes/profiles`: `council-planner-routine`, `council-planner-standard`, `council-planner-complex`, `council-planner-critical`, `council-coder-routine`, `council-coder-standard`, `council-coder-complex`, `council-coder-critical`, `council-tester`, and `council-reviewer`.

## Bootstrap

From the checked-out `flyash-phreeqc-ml` directory on the Hermes computer:

```bash
export WPI_AI_COUNCIL_ROOT='/absolute/path/to/live/AI-Council'
./scripts/install-council-operator.sh
```

The installer checks the architecture, Python, Docker, exact Council contract hashes, outboxes, and all required profile files; installs the local package without dependencies; preserves an existing config; and otherwise creates a mode-`0600` template at `~/.config/wpi-virtual-lab/council.toml`. When a completed config already exists, it also runs the full worker doctor, including Git remote authentication. Replace every placeholder with the approved local paths/remotes, explicitly set `control_remote_private` only after checking visibility, and set `hermes_executable` to the exact existing binary.

One exact acceptance command, after the config has been completed, is:

```bash
WPI_AI_COUNCIL_ROOT='/absolute/path/to/live/AI-Council' ./scripts/check-council-operator.sh
```

That command verifies the tracked policy, live checkout, Git code/control remote read authentication, exact SHA-256 identities of all authoritative Council contracts, role outboxes, all launcher-selected profile files, Docker, and the explicitly configured Hermes executable. It does not write repository history. A successful result means worker preflight passed; it is not evidence that a live four-role task has run.

## Run a task

After a controller submits a task:

```bash
./scripts/run-hermes-task.sh <task-id>
```

This performs worker preflight, acquires the remote lock, and executes the bounded Hermes pipeline. For background operation:

```bash
./scripts/start-hermes-worker.sh <task-id>
./scripts/stop-hermes-worker.sh
```

Logs and PID state are under `${XDG_STATE_HOME:-$HOME/.local/state}/wpi-council`. Output is bounded by operator policy; authoritative state is in the configured control remote, not the PID file. Use `wpi-council status <task-id>` from either computer to inspect progress.

## Acceptance boundary

No approved SSH alias, mount, or operator endpoint was assumed by the project package, and it never scans the network. When the Hermes computer is available, first run the acceptance command above, then submit a sanitized documentation-only task and verify the complete Planner/Coder/Tester/Reviewer path, normal task-branch push, controller monitoring, and final `awaiting_human_review` state. Record that as live evidence only after the physical worker completed it.

Until then, the repository's deterministic two-clone/bare-remote journeys are simulation evidence, not a live Hermes acceptance claim.
