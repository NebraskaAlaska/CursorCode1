#!/bin/sh
set -eu

# Resolve exactly one trusted Python 3.12 interpreter.  Installation uses the
# documented priority order.  Routine operator commands use --require-record so
# a later PATH change can never silently select a different environment.

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
default_project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
project_dir=$default_project_dir
project_was_explicit=false
runtime_record=
require_record=false

usage() {
  echo "usage: resolve-council-python.sh [--project-root PATH] [--runtime-record PATH] [--require-record]" >&2
  exit 2
}

while test "$#" -gt 0; do
  case "$1" in
    --project-root)
      test "$#" -ge 2 || usage
      project_dir=$2
      project_was_explicit=true
      shift 2
      ;;
    --runtime-record)
      test "$#" -ge 2 || usage
      runtime_record=$2
      shift 2
      ;;
    --require-record)
      require_record=true
      shift
      ;;
    *) usage ;;
  esac
done

case "$project_dir" in
  /*) ;;
  *) echo "Council project root must be an absolute path." >&2; exit 1 ;;
esac

installation_instruction() {
  echo "No trusted Python 3.12 interpreter was found. Install Python 3.12, then run: /absolute/path/to/python3.12 -m venv \"$project_dir/.venv\"" >&2
  exit 1
}

record_error() {
  echo "The WPI Council Python runtime record is missing, malformed, incompatible, or no longer matches its interpreter." >&2
  exit 1
}

file_mode() {
  mode_path=$1
  if stat -f '%Lp' "$mode_path" >/dev/null 2>&1; then
    stat -f '%Lp' "$mode_path"
  else
    stat -c '%a' "$mode_path" 2>/dev/null
  fi
}

verified_executable() {
  candidate=$1
  case "$candidate" in /*) ;; *) return 1 ;; esac
  test -f "$candidate" && test -x "$candidate" || return 1
  observed=$(
    "$candidate" -c 'import os, sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit(1)
value = os.path.abspath(sys.executable)
if not os.path.isabs(value):
    raise SystemExit(1)
print(value)' 2>/dev/null
  ) || return 1
  case "$observed" in
    /*) ;;
    *) return 1 ;;
  esac
  case "$observed" in
    *"
"*) return 1 ;;
  esac
  test -f "$observed" && test -x "$observed" || return 1
  "$observed" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
    >/dev/null 2>&1 || return 1
  printf '%s\n' "$observed"
}

verify_record() {
  record_path=$1
  case "$record_path" in /*) ;; *) record_error ;; esac
  test -f "$record_path" && test ! -L "$record_path" || record_error
  test "$(file_mode "$record_path")" = 600 || record_error
  test "$(awk 'END { print NR }' "$record_path")" = 5 || record_error

  record_schema=$(sed -n '1p' "$record_path")
  record_python=$(sed -n '2p' "$record_path")
  record_version=$(sed -n '3p' "$record_path")
  record_sha256=$(sed -n '4p' "$record_path")
  record_project=$(sed -n '5p' "$record_path")
  test "$record_schema" = "wpi-council-python-runtime/v1" || record_error
  case "$record_python" in /*) ;; *) record_error ;; esac
  case "$record_version" in 3.12.*) ;; *) record_error ;; esac
  case "$record_sha256" in
    *[!0-9a-f]*|'') record_error ;;
  esac
  test "${#record_sha256}" -eq 64 || record_error
  case "$record_project" in /*) ;; *) record_error ;; esac
  if test "$project_was_explicit" = true; then
    test "$record_project" = "$project_dir" || record_error
  fi

  verified=$(verified_executable "$record_python") || record_error
  test "$verified" = "$record_python" || record_error
  observed_version=$("$record_python" -c 'import platform; print(platform.python_version())') || record_error
  test "$observed_version" = "$record_version" || record_error
  observed_sha256=$("$record_python" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.executable).read_bytes()).hexdigest())') || record_error
  test "$observed_sha256" = "$record_sha256" || record_error
  printf '%s\n' "$record_python"
}

if test -n "$runtime_record" && test -e "$runtime_record"; then
  verify_record "$runtime_record"
  exit 0
fi
if test "$require_record" = true; then
  record_error
fi

if test -n "${WPI_COUNCIL_PYTHON:-}"; then
  case "$WPI_COUNCIL_PYTHON" in /*) ;; *) installation_instruction ;; esac
  verified_executable "$WPI_COUNCIL_PYTHON" || installation_instruction
  exit 0
fi

project_python="$project_dir/.venv/bin/python"
if test -x "$project_python"; then
  if verified_executable "$project_python"; then
    exit 0
  fi
fi

python312=$(command -v python3.12 2>/dev/null || true)
if test -n "$python312"; then
  case "$python312" in /*) ;; *) installation_instruction ;; esac
  verified_executable "$python312" || installation_instruction
  exit 0
fi

installation_instruction
