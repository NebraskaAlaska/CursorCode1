#!/bin/sh
set -eu
test "$#" -eq 1 || { echo "usage: start-hermes-worker.sh TASK_ID" >&2; exit 2; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
runtime_record="${WPI_COUNCIL_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/wpi-council}/operator-python.runtime"
python_bin=$("$script_dir/resolve-council-python.sh" \
  --project-root "$project_dir" \
  --runtime-record "$runtime_record" \
  --require-record)
case "$python_bin" in
  /*) ;;
  *) echo "Council Python resolver did not return an absolute path." >&2; exit 1 ;;
esac
exec "$python_bin" -m flyash_phreeqc_ml.council_operator.worker_supervisor start "$1"
