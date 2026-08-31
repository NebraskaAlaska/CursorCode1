#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
user_home=${HOME:?HOME must identify the current user}
install_root=${WPI_COUNCIL_INSTALL_ROOT:-${XDG_DATA_HOME:-"$user_home/.local/share"}/wpi-council}
runtime_record="$install_root/operator-python.runtime"
python_bin=$(
  "$script_dir/resolve-council-python.sh" \
    --project-root "$project_dir" \
    --runtime-record "$runtime_record" \
    --require-record
)
exec "$python_bin" -m flyash_phreeqc_ml.council_operator.cli doctor --worker "$@"
