#!/bin/sh
set -eu
test "$#" -eq 1 || { echo "usage: start-hermes-worker.sh TASK_ID" >&2; exit 2; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
state_dir=${XDG_STATE_HOME:-"$HOME/.local/state"}/wpi-council
pid_file="$state_dir/hermes-worker.pid"
log_file="$state_dir/hermes-worker.log"
mkdir -p "$state_dir"
if test -s "$pid_file" && kill -0 "$(sed -n '1p' "$pid_file")" 2>/dev/null; then
  echo "Hermes worker is already running." >&2
  exit 1
fi
nohup "$script_dir/run-hermes-task.sh" "$1" >"$log_file" 2>&1 &
worker_pid=$!
printf '%s\n' "$worker_pid" >"$pid_file"
echo "Hermes worker started for task $1 (PID $worker_pid)."
