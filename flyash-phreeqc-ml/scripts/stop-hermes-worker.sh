#!/bin/sh
set -eu
state_dir=${XDG_STATE_HOME:-"$HOME/.local/state"}/wpi-council
pid_file="$state_dir/hermes-worker.pid"
test -s "$pid_file" || { echo "No Hermes worker PID file exists."; exit 0; }
worker_pid=$(sed -n '1p' "$pid_file")
case "$worker_pid" in *[!0-9]*|'') echo "Unsafe worker PID file." >&2; exit 1 ;; esac
if kill -0 "$worker_pid" 2>/dev/null; then
  kill "$worker_pid"
fi
rm -f "$pid_file"
echo "Hermes worker stop requested. The task lease remains recoverable."
