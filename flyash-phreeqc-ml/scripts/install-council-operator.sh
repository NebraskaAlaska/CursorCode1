#!/bin/sh
set -eu

umask 077
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
resolver="$script_dir/resolve-council-python.sh"
pip_bootstrap_helper="$script_dir/bootstrap_council_pip.py"
user_home=${HOME:?HOME must identify the current user}
config_dir=${XDG_CONFIG_HOME:-"$user_home/.config"}/wpi-virtual-lab
config_path=${WPI_COUNCIL_CONFIG:-"$config_dir/council.toml"}
config_parent=$(dirname -- "$config_path")
install_root=${WPI_COUNCIL_INSTALL_ROOT:-${XDG_DATA_HOME:-"$user_home/.local/share"}/wpi-council}
runtime_record="$install_root/operator-python.runtime"
installed_resolver="$install_root/libexec/resolve-council-python.sh"
launcher_path="$install_root/bin/wpi-council"
public_bin_dir=${WPI_COUNCIL_BIN_DIR:-${XDG_BIN_HOME:-"$user_home/.local/bin"}}
public_launcher="$public_bin_dir/wpi-council"

fail() {
  echo "$1" >&2
  exit 1
}

require_safe_absolute_path() {
  path_value=$1
  path_label=$2
  case "$path_value" in
    /*) ;;
    *) fail "$path_label must be an absolute path." ;;
  esac
  test "$path_value" != / || fail "$path_label cannot be the filesystem root."
  case "$path_value" in
    *"
"*) fail "$path_label cannot contain a newline." ;;
  esac
}

file_mode() {
  mode_path=$1
  if stat -f '%Lp' "$mode_path" >/dev/null 2>&1; then
    stat -f '%Lp' "$mode_path"
  else
    stat -c '%a' "$mode_path" 2>/dev/null
  fi
}

file_owner() {
  owner_path=$1
  if stat -f '%u' "$owner_path" >/dev/null 2>&1; then
    stat -f '%u' "$owner_path"
  else
    stat -c '%u' "$owner_path" 2>/dev/null
  fi
}

safe_owned_directory() {
  directory_path=$1
  test -d "$directory_path" && test ! -L "$directory_path" || return 1
  test "$(file_owner "$directory_path")" = "$(id -u)" || return 1
  directory_mode=$(file_mode "$directory_path") || return 1
  case "${#directory_mode}" in
    3) permission_mode=$directory_mode ;;
    4) permission_mode=${directory_mode#?} ;;
    *) return 1 ;;
  esac
  group_and_other=${permission_mode#?}
  group_permission=${group_and_other%?}
  other_permission=${group_and_other#?}
  case "$group_permission:$other_permission" in
    2:*|3:*|6:*|7:*|*:2|*:3|*:6|*:7) return 1 ;;
  esac
}

safe_runtime_record_for_bootstrap() {
  case "$install_root" in
    "$user_home"/*) ;;
    *) return 1 ;;
  esac
  safe_owned_directory "$user_home" || return 1
  test "$(CDPATH= cd -- "$user_home" && pwd -P)" = "$user_home" || return 1

  remaining_path=${install_root#"$user_home"/}
  current_path=$user_home
  while test -n "$remaining_path"; do
    case "$remaining_path" in
      */*) path_component=${remaining_path%%/*}; remaining_path=${remaining_path#*/} ;;
      *) path_component=$remaining_path; remaining_path= ;;
    esac
    case "$path_component" in ''|.|..) return 1 ;; esac
    current_path=$current_path/$path_component
    safe_owned_directory "$current_path" || return 1
  done

  test -f "$runtime_record" && test ! -L "$runtime_record" || return 1
  test "$(file_owner "$runtime_record")" = "$(id -u)" || return 1
  test "$(file_mode "$runtime_record")" = 600 || return 1
}

directory_is_empty() {
  empty_directory=$1
  for existing_entry in \
    "$empty_directory"/* \
    "$empty_directory"/.[!.]* \
    "$empty_directory"/..?*; do
    if test -e "$existing_entry" || test -L "$existing_entry"; then
      return 1
    fi
  done
}

check_user_local_directories() {
  create_missing=$1
  shift
  "$bootstrap_python" - "$user_home" "$create_missing" "$@" <<'PY'
import os
from pathlib import Path
import stat
import sys

home = Path(os.path.abspath(sys.argv[1]))
create_missing = sys.argv[2] == "true"
targets = [Path(value) for value in sys.argv[3:]]

try:
    home_info = home.lstat()
except OSError as exc:
    raise SystemExit("HOME is not a safe user-local directory.") from exc
if (
    not stat.S_ISDIR(home_info.st_mode)
    or stat.S_ISLNK(home_info.st_mode)
    or home.resolve(strict=True) != home
    or home_info.st_uid != os.getuid()
    or stat.S_IMODE(home_info.st_mode) & 0o022
):
    raise SystemExit("HOME is not a safe user-local directory.")

for raw_target in targets:
    if ".." in raw_target.parts:
        raise SystemExit("Operator paths cannot contain parent traversal.")
    target = Path(os.path.abspath(raw_target))
    try:
        relative = target.relative_to(home)
    except ValueError as exc:
        raise SystemExit("Operator install, launcher, and config directories must remain beneath HOME.") from exc
    if not relative.parts:
        raise SystemExit("HOME itself cannot be used as an operator state directory.")
    current = home
    for component in relative.parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create_missing:
                break
            current.mkdir(mode=0o700)
            info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise SystemExit(
                "Operator user-local directories must be real, current-user-owned, and not group/world-writable."
            )
PY
}

require_safe_absolute_path "$project_dir" "Project root"
require_safe_absolute_path "$config_path" "Operator config path"
require_safe_absolute_path "$config_parent" "Operator config directory"
require_safe_absolute_path "$install_root" "Operator installation root"
require_safe_absolute_path "$public_bin_dir" "Operator launcher directory"
test -x "$resolver" || fail "The shared Council Python resolver is unavailable."
test -f "$pip_bootstrap_helper" && test ! -L "$pip_bootstrap_helper" || fail "The trusted Council pip bootstrap helper is unavailable."

test "$(uname -s)" = Darwin || fail "This bootstrap package currently supports macOS workers."
architecture=$(uname -m)
case "$architecture" in
  arm64|x86_64) ;;
  *) fail "Unsupported macOS architecture: $architecture" ;;
esac

bootstrap_from_record=false
bootstrap_recorded_python=
if test -e "$runtime_record" || test -L "$runtime_record"; then
  safe_runtime_record_for_bootstrap || fail "Existing operator installation is incompatible; it was preserved."
  if bootstrap_recorded_python=$(
    "$resolver" --project-root "$project_dir" --runtime-record "$runtime_record" --require-record
  ); then
    :
  else
    fail "Existing operator installation is incompatible; it was preserved."
  fi
  if test -n "${WPI_COUNCIL_PYTHON:-}"; then
    explicitly_requested_python=$("$resolver" --project-root "$project_dir")
    test "$explicitly_requested_python" = "$bootstrap_recorded_python" || fail "Existing operator installation uses a different Python; it was preserved."
  fi
  bootstrap_python=$bootstrap_recorded_python
  bootstrap_from_record=true
else
  bootstrap_python=$("$resolver" --project-root "$project_dir")
fi
bootstrap_is_venv=$(
  "$bootstrap_python" -c 'import sys; print("true" if sys.prefix != sys.base_prefix else "false")'
)
case "$bootstrap_is_venv" in true|false) ;; *) fail "Unable to classify the verified Python 3.12 runtime." ;; esac
check_user_local_directories false "$config_parent" "$install_root" "$public_bin_dir"

command -v docker >/dev/null 2>&1 || fail "Docker is required."
docker version >/dev/null 2>&1 || fail "Docker is installed but unavailable."
test -n "${WPI_AI_COUNCIL_ROOT:-}" || fail "Set WPI_AI_COUNCIL_ROOT to the existing live Council root."
require_safe_absolute_path "$WPI_AI_COUNCIL_ROOT" "Live Council root"
for required in \
  AGENTS.md START_HERE.md \
  prompts/START_PLANNER.md prompts/START_CODER.md prompts/START_TESTER.md prompts/START_REVIEWER.md \
  tools/council_route.py tools/council_stage_launcher.py tools/council_patch_gate.py tools/coder_export_bundle.py; do
  test -f "$WPI_AI_COUNCIL_ROOT/$required" || fail "Council contract is missing: $required"
done

# Verify the exact, tracked Council contract from source before writing any
# operator installation state. The compatibility layer has only stdlib imports.
(
  cd "$project_dir"
  "$bootstrap_python" -c 'from pathlib import Path
import sys
from flyash_phreeqc_ml.council_operator.backends import CouncilCompatibilityAdapter
from flyash_phreeqc_ml.council_operator.contracts import ProjectPolicy
policy = ProjectPolicy.load(Path(sys.argv[1]))
observed = CouncilCompatibilityAdapter(Path(sys.argv[2]), policy).verify()
print("Verified authoritative Council SHA-256 contracts and role outboxes:", len(observed))' \
    "$project_dir/config/council_operator_policy.toml" "$WPI_AI_COUNCIL_ROOT"
)

for profile in \
  council-planner-routine council-planner-standard council-planner-complex council-planner-critical \
  council-coder-routine council-coder-standard council-coder-complex council-coder-critical \
  council-tester council-reviewer; do
  profile_root="$user_home/.hermes/profiles/$profile"
  test -d "$profile_root" || fail "Hermes profile is missing: $profile"
  for profile_file in config.yaml SOUL.md state.db; do
    test -f "$profile_root/$profile_file" || fail "Hermes profile file is missing: $profile/$profile_file"
  done
  echo "Verified required Hermes profile: $profile"
done

"$bootstrap_python" "$project_dir/scripts/validate_dependency_lock.py"

# Refuse unsafe or incompatible existing local state before any package or
# environment mutation. A valid record is the sole authority for a reusable
# installation.
config_exists=false
if test -e "$config_path" || test -L "$config_path"; then
  test -f "$config_path" && test ! -L "$config_path" || fail "Existing operator config is not a regular file; it was preserved."
  test "$(file_mode "$config_path")" = 600 || fail "Existing operator config must have mode 0600; it was preserved."
  config_exists=true
fi
test ! -L "$install_root" || fail "Operator installation root cannot be a symbolic link."
test ! -L "$installed_resolver" || fail "Existing operator resolver is incompatible; it was preserved."
test ! -L "$launcher_path" || fail "Existing operator launcher is incompatible; it was preserved."
if test "$public_launcher" != "$launcher_path" && { test -e "$public_launcher" || test -L "$public_launcher"; }; then
  test -L "$public_launcher" || fail "Existing wpi-council launcher is incompatible; it was preserved."
  test "$(readlink "$public_launcher")" = "$launcher_path" || fail "Existing wpi-council launcher is incompatible; it was preserved."
fi

# Determine the final path without creating it so an incompatible record cannot
# cause even a partial environment write.  An existing verified runtime record
# remains authoritative across later checkout changes (for example, creation of
# a project .venv).  An explicit interpreter override is an operator request and
# therefore must still identify that same recorded runtime.
operator_venv="$install_root/venv"
created_operator_venv=false
cleanup_new_operator_environment() {
  if test "$created_operator_venv" = true; then
    rm -rf "$operator_venv"
  fi
}
recorded_install=false
if test -e "$runtime_record" || test -L "$runtime_record"; then
  if recorded_python=$(
    "$resolver" --project-root "$project_dir" --runtime-record "$runtime_record" --require-record
  ); then
    :
  else
    fail "Existing operator installation is incompatible; it was preserved."
  fi
  if test "$bootstrap_from_record" = true; then
    test "$recorded_python" = "$bootstrap_recorded_python" || fail "Existing operator installation changed during verification; it was preserved."
  fi
  if test -n "${WPI_COUNCIL_PYTHON:-}"; then
    test "$recorded_python" = "$bootstrap_python" || fail "Existing operator installation uses a different Python; it was preserved."
  fi
  operator_python=$recorded_python
  recorded_is_venv=$(
    "$operator_python" -c 'import sys; print("true" if sys.prefix != sys.base_prefix else "false")'
  )
  test "$recorded_is_venv" = true || fail "Existing operator installation is incompatible; it was preserved."
  recorded_install=true
else
  if test "$bootstrap_is_venv" = true; then
    operator_python=$bootstrap_python
  else
    operator_python="$operator_venv/bin/python"
  fi
  if test -e "$install_root" || test -L "$install_root"; then
    test -d "$install_root" && test ! -L "$install_root" || fail "Existing operator installation is incomplete or incompatible; it was preserved."
    directory_is_empty "$install_root" || fail "Existing operator installation is incomplete or incompatible; it was preserved."
  fi
fi

# A verified virtual environment is installed in place. A base interpreter is
# used only to create a dedicated user-local environment; the base environment
# is never changed.
if test "$recorded_install" = false && test "$bootstrap_is_venv" = false; then
  if test -e "$operator_venv" || test -L "$operator_venv"; then
    test -d "$operator_venv" && test ! -L "$operator_venv" || fail "Existing operator environment is incompatible; it was preserved."
    test -x "$operator_venv/bin/python" || fail "Existing operator environment is incompatible; it was preserved."
  else
    check_user_local_directories true "$install_root"
    created_operator_venv=true
    trap cleanup_new_operator_environment 0 1 2 3 15
    "$bootstrap_python" -m venv "$operator_venv"
  fi
  operator_python=$(env WPI_COUNCIL_PYTHON="$operator_venv/bin/python" "$resolver" --project-root "$project_dir")
  operator_is_venv=$(
    "$operator_python" -c 'import sys; print("true" if sys.prefix != sys.base_prefix else "false")'
  )
  test "$operator_is_venv" = true || fail "Dedicated operator environment is incompatible; it was preserved."
fi

valid_installed_runtime() {
  selected_operator_python "$project_dir/scripts/validate_dependency_lock.py" --installed >/dev/null 2>&1 || return 1
  selected_operator_python -c 'from pathlib import Path
import sys
import anthropic
import joblib
import matplotlib
import numpy
import openpyxl
import pandas
import pytest
import scipy
import sklearn
import streamlit
import xlrd
import flyash_phreeqc_ml
from flyash_phreeqc_ml.council_operator.cli import main
expected = Path(sys.argv[1]).resolve() / "flyash_phreeqc_ml"
observed = Path(flyash_phreeqc_ml.__file__).resolve().parent
raise SystemExit(0 if observed == expected and callable(main) else 1)' "$project_dir" >/dev/null 2>&1
}

selected_operator_python() {
  env \
    -u PIP_CONSTRAINT \
    -u PIP_EDITABLE \
    -u PIP_LOG \
    -u PIP_PREFIX \
    -u PIP_REPORT \
    -u PIP_REQUIREMENT \
    -u PIP_ROOT \
    -u PIP_TARGET \
    -u PIP_USER \
    -u PYTHONUSERBASE \
    -u VIRTUAL_ENV \
    PIP_CONFIG_FILE=/dev/null \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHOME= \
    PYTHONNOUSERSITE=1 \
    PYTHONPATH= \
    "$operator_python" -I "$@"
}

verified_setuptools_identity() {
  selected_operator_python -c 'from importlib import metadata
import json
from pathlib import Path
import setuptools
import setuptools.build_meta
import sys
import tomllib

declaration = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["build-system"]["requires"]
if declaration != ["setuptools==83.0.0"]:
    raise SystemExit(1)
expected = declaration[0].split("==", 1)[1]
prefix = Path(sys.prefix).resolve()
distribution = metadata.distribution("setuptools")
distribution_root = Path(distribution.locate_file("")).resolve()
module_path = Path(setuptools.__file__).resolve()
if distribution.version != expected:
    raise SystemExit(1)
if distribution_root != prefix and prefix not in distribution_root.parents:
    raise SystemExit(1)
if module_path != prefix and prefix not in module_path.parents:
    raise SystemExit(1)
print(json.dumps({
    "distribution_root": str(distribution_root),
    "module": str(module_path),
    "version": distribution.version,
}, sort_keys=True, separators=(",", ":")))' "$project_dir/pyproject.toml"
}

pip_bootstrap_evidence=
setuptools_identity=
if test "$recorded_install" = true; then
  if pip_bootstrap_evidence=$(selected_operator_python "$pip_bootstrap_helper" --verify-only 2>/dev/null); then
    :
  else
    fail "Existing operator installation is incompatible; it was preserved."
  fi
  setuptools_identity=$(verified_setuptools_identity) || fail "Existing operator installation is incompatible; it was preserved."
  valid_installed_runtime || fail "Existing operator installation is incompatible; it was preserved."
  echo "Existing valid operator Python installation preserved: $operator_python"
else
  if pip_bootstrap_evidence=$(selected_operator_python "$pip_bootstrap_helper"); then
    :
  else
    exit 1
  fi
  selected_operator_python -m pip install --disable-pip-version-check \
    --constraint "$project_dir/constraints-py312.txt" \
    setuptools==83.0.0 \
    --requirement "$project_dir/requirements-dev.txt"
  setuptools_identity=$(verified_setuptools_identity) || fail "The exact setuptools==83.0.0 build requirement is unavailable in the selected operator environment."
  selected_operator_python -m pip install --disable-pip-version-check \
    --no-build-isolation --no-deps --editable "$project_dir"
  selected_operator_python "$project_dir/scripts/validate_dependency_lock.py" --installed
  valid_installed_runtime || fail "Required operator imports or pinned dependencies could not be verified."
fi

operator_version=$(
  "$operator_python" -c 'import platform, sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit(1)
print(platform.python_version())'
)
operator_sha256=$(
  "$operator_python" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.executable).read_bytes()).hexdigest())'
)
case "$operator_version" in 3.12.*) ;; *) fail "Installed operator Python is not exactly Python 3.12." ;; esac
case "$operator_sha256" in *[!0-9a-f]*|'') fail "Installed operator Python identity is invalid." ;; esac
test "${#operator_sha256}" -eq 64 || fail "Installed operator Python identity is invalid."

if test "$created_operator_venv" = true; then
  created_operator_venv=false
  trap - 0 1 2 3 15
fi

check_user_local_directories true "$config_parent" "$install_root" "$public_bin_dir"

mkdir -p "$config_parent"
if test "$config_exists" = true; then
  echo "Existing operator config preserved: $config_path"
else
  chmod 700 "$config_parent"
  config_tmp="$config_path.tmp.$$"
  trap 'rm -f "$config_tmp"' 0 1 2 3 15
  cp "$project_dir/config/council_operator.example.toml" "$config_tmp"
  chmod 600 "$config_tmp"
  mv "$config_tmp" "$config_path"
  trap - 0 1 2 3 15
  echo "Created config template: $config_path"
  echo "Replace placeholders, then run check-council-operator.sh."
fi

mkdir -p "$install_root/libexec" "$install_root/bin" "$public_bin_dir"

if test ! -f "$installed_resolver" || ! cmp -s "$resolver" "$installed_resolver"; then
  resolver_tmp="$installed_resolver.tmp.$$"
  trap 'rm -f "$resolver_tmp"' 0 1 2 3 15
  cp "$resolver" "$resolver_tmp"
  chmod 755 "$resolver_tmp"
  mv "$resolver_tmp" "$installed_resolver"
  trap - 0 1 2 3 15
fi

launcher_tmp="$launcher_path.tmp.$$"
trap 'rm -f "$launcher_tmp"' 0 1 2 3 15
cat >"$launcher_tmp" <<'LAUNCHER'
#!/bin/sh
set -eu
program=$0
case "$program" in /*) ;; *) program=$PWD/$program ;; esac
links=0
while test -L "$program"; do
  links=$((links + 1))
  test "$links" -le 16 || { echo "wpi-council launcher symlink chain is unsafe." >&2; exit 1; }
  link=$(readlink "$program")
  case "$link" in
    /*) program=$link ;;
    *) program=$(dirname -- "$program")/$link ;;
  esac
done
launcher_dir=$(CDPATH= cd -- "$(dirname -- "$program")" && pwd -P)
operator_root=$(CDPATH= cd -- "$launcher_dir/.." && pwd -P)
runtime_record="$operator_root/operator-python.runtime"
resolver="$operator_root/libexec/resolve-council-python.sh"
python_bin=$("$resolver" --runtime-record "$runtime_record" --require-record)
exec "$python_bin" -m flyash_phreeqc_ml.council_operator.cli "$@"
LAUNCHER
chmod 755 "$launcher_tmp"
if test -f "$launcher_path" && cmp -s "$launcher_tmp" "$launcher_path"; then
  rm -f "$launcher_tmp"
else
  test ! -L "$launcher_path" || fail "Installed operator launcher is incompatible; it was preserved."
  mv "$launcher_tmp" "$launcher_path"
fi
trap - 0 1 2 3 15

if test "$recorded_install" = false; then
  record_tmp="$runtime_record.tmp.$$"
  trap 'rm -f "$record_tmp"' 0 1 2 3 15
  printf '%s\n%s\n%s\n%s\n%s\n' \
    'wpi-council-python-runtime/v1' \
    "$operator_python" \
    "$operator_version" \
    "$operator_sha256" \
    "$project_dir" >"$record_tmp"
  chmod 600 "$record_tmp"
  mv "$record_tmp" "$runtime_record"
  trap - 0 1 2 3 15
fi

"$installed_resolver" --runtime-record "$runtime_record" --require-record >/dev/null
if test "$public_launcher" != "$launcher_path" && test ! -e "$public_launcher" && test ! -L "$public_launcher"; then
  ln -s "$launcher_path" "$public_launcher"
fi

echo "Recorded operator Python 3.12: $operator_python"
echo "Recorded operator Python version: $operator_version"
echo "Recorded operator Python SHA-256: $operator_sha256"
if test -n "$pip_bootstrap_evidence"; then
  echo "Verified operator pip bootstrap: $pip_bootstrap_evidence"
fi
if test -n "$setuptools_identity"; then
  echo "Verified operator build toolchain: $setuptools_identity"
fi
echo "Stable launcher: $public_launcher"
echo "Installed WPI Council operator for macOS $architecture without modifying Council control files."
