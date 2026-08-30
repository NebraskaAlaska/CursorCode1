#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/wpi-virtual-lab
config_path=${WPI_COUNCIL_CONFIG:-"$config_dir/council.toml"}

test "$(uname -s)" = Darwin || {
  echo "This bootstrap package currently supports macOS workers." >&2
  exit 1
}
architecture=$(uname -m)
case "$architecture" in
  arm64|x86_64) ;;
  *) echo "Unsupported macOS architecture: $architecture" >&2; exit 1 ;;
esac

python_bin=${WPI_COUNCIL_PYTHON:-python3}
"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' || {
  echo "Python 3.12 is required." >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 1; }
docker version >/dev/null 2>&1 || { echo "Docker is installed but unavailable." >&2; exit 1; }
test -n "${WPI_AI_COUNCIL_ROOT:-}" || {
  echo "Set WPI_AI_COUNCIL_ROOT to the existing live Council root." >&2
  exit 1
}
for required in AGENTS.md START_HERE.md tools/council_route.py tools/council_stage_launcher.py tools/council_patch_gate.py tools/coder_export_bundle.py; do
  test -f "$WPI_AI_COUNCIL_ROOT/$required" || { echo "Council contract is missing: $required" >&2; exit 1; }
done

"$python_bin" -m pip install --user --no-deps "$project_dir"
"$python_bin" -c 'from pathlib import Path; import sys; from flyash_phreeqc_ml.council_operator.backends import CouncilCompatibilityAdapter; from flyash_phreeqc_ml.council_operator.contracts import ProjectPolicy; policy=ProjectPolicy.load(Path(sys.argv[1])); observed=CouncilCompatibilityAdapter(Path(sys.argv[2]), policy).verify(); print("Verified authoritative Council SHA-256 contracts and role outboxes:", len(observed))' "$project_dir/config/council_operator_policy.toml" "$WPI_AI_COUNCIL_ROOT"

for profile in \
  council-planner-routine council-planner-standard council-planner-complex council-planner-critical \
  council-coder-routine council-coder-standard council-coder-complex council-coder-critical \
  council-tester council-reviewer; do
  profile_root="$HOME/.hermes/profiles/$profile"
  test -d "$profile_root" || { echo "Hermes profile is missing: $profile" >&2; exit 1; }
  for profile_file in config.yaml SOUL.md state.db; do
    test -f "$profile_root/$profile_file" || { echo "Hermes profile file is missing: $profile/$profile_file" >&2; exit 1; }
  done
  echo "Verified required Hermes profile: $profile"
done

mkdir -p "$config_dir"
if test -e "$config_path"; then
  echo "Existing operator config preserved: $config_path"
  "$script_dir/check-council-operator.sh"
else
  cp "$project_dir/config/council_operator.example.toml" "$config_path"
  chmod 600 "$config_path"
  echo "Created config template: $config_path"
  echo "Replace placeholders, then rerun this installer or check-council-operator.sh."
fi

echo "Installed WPI Council operator for macOS $architecture without modifying Council control files."
