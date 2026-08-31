#!/bin/sh
set -eu
python_bin=${WPI_COUNCIL_PYTHON:-python3}
exec "$python_bin" -m flyash_phreeqc_ml.council_operator.cli doctor --worker "$@"
