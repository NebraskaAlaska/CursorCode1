#!/bin/sh
set -eu
test "$#" -ge 1 || { echo "usage: run-hermes-task.sh TASK_ID" >&2; exit 2; }
task_id=$1
shift
python_bin=${WPI_COUNCIL_PYTHON:-python3}
"$python_bin" -m flyash_phreeqc_ml.council_operator.cli doctor --worker
"$python_bin" -m flyash_phreeqc_ml.council_operator.cli claim "$task_id"
exec "$python_bin" -m flyash_phreeqc_ml.council_operator.cli run "$task_id" --backend hermes --overnight "$@"
