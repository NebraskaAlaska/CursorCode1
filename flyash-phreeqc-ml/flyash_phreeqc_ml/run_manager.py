"""Experiment Run Manager — lightweight "save files" for different experiments.

This is an **app-level save/open layer**, not a project-management system. It lets
one app hold several independent experiment runs (e.g. a pH-only lab run, a
literature-benchmark demo, future ICP data, a plastic/fly-ash side project), each
in its own folder so their data never get mixed up.

Layout (under ``config.EXPERIMENT_RUNS_DIR`` = ``<repo>/experiments/``)::

    experiments/<safe_run_name>/
        run_config.yaml
        data/
        outputs/

The data file inside each run depends on the run *type*:

    lab_experiment       data/experimental_release.csv   (canonical release schema)
    plastic_composite    data/experimental_release.csv   (same schema, side project)
    literature_benchmark data/literature_benchmark.csv   (literature schema)
    synthetic_demo       data/demo_data.csv              (release schema + source_type)

Design rules that keep the data honest:

* Literature data can **never** be written to a lab run's
  ``experimental_release.csv`` — :func:`require_run_type` guards every typed path,
  so a literature run only writes ``literature_benchmark.csv``.
* This layer does not touch the existing ``data/raw/experimental_icp`` pipeline.
  :func:`export_lab_run_to_pipeline` is an explicit, opt-in copy from a lab run
  into the manual-entry file the scripts already read.

No chemistry or ML logic lives here. YAML is read/written with a tiny built-in
helper (JSON-quoted scalars are valid YAML), so the package needs no new
dependency.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
RUN_TYPES = [
    "lab_experiment",
    "literature_benchmark",
    "synthetic_demo",
    "plastic_composite",
]

DATA_SOURCES = ["experimental", "literature", "synthetic"]

RUN_CONFIG_FILENAME = "run_config.yaml"

# Tag stamped on every synthetic_demo row so demo data can never be mistaken for
# real measurements once exported.
SYNTHETIC_SOURCE_TAG = "synthetic_demo"

# Literature-benchmark schema. Deliberately separate from the measured-release
# schema: literature values are *reported by other papers*, not measured by us.
LITERATURE_BENCHMARK_COLUMNS = [
    "source_id",
    "paper_title",
    "authors",
    "year",
    "DOI_or_URL",
    "fly_ash_class",
    "fly_ash_source",
    "leachant",
    "NaOH_M",
    "time_min",
    "temperature_C",
    "liquid_solid_ratio",
    "CO2_condition",
    "reported_initial_pH",
    "reported_final_pH",
    "reported_Ca_mM",
    "reported_Si_mM",
    "reported_Al_mM",
    "reported_Fe_mM",
    "reported_Na_mM",
    "reported_K_mM",
    "reported_Sc_ppb",
    "reported_total_REE_ppb",
    "reported_REE_recovery_percent",
    "reported_Sc_recovery_percent",
    "method_notes",
    "comparability_to_our_experiment",
    "notes",
]

# Demo data mirrors the real release schema (so it can exercise the pipeline) but
# carries a leading source_type tag marking every row as synthetic.
DEMO_DATA_COLUMNS = ["source_type"] + config.EXPERIMENTAL_RELEASE_COLUMNS


@dataclass(frozen=True)
class RunTypeSpec:
    data_source: str
    data_filename: str
    columns: list[str]
    warning: str


# Per-run-type behaviour, in one place so the app and the module agree.
RUN_TYPE_SPECS: dict[str, RunTypeSpec] = {
    "lab_experiment": RunTypeSpec(
        data_source="experimental",
        data_filename="experimental_release.csv",
        columns=list(config.EXPERIMENTAL_RELEASE_COLUMNS),
        warning="This run contains real measured lab data.",
    ),
    "literature_benchmark": RunTypeSpec(
        data_source="literature",
        data_filename="literature_benchmark.csv",
        columns=list(LITERATURE_BENCHMARK_COLUMNS),
        warning=(
            "This run contains literature data only. "
            "Do not treat this as our measured experiment."
        ),
    ),
    "synthetic_demo": RunTypeSpec(
        data_source="synthetic",
        data_filename="demo_data.csv",
        columns=list(DEMO_DATA_COLUMNS),
        warning=(
            "This run contains synthetic/demo data only. "
            "It is for testing code, not scientific conclusions."
        ),
    ),
    "plastic_composite": RunTypeSpec(
        data_source="experimental",
        data_filename="experimental_release.csv",
        columns=list(config.EXPERIMENTAL_RELEASE_COLUMNS),
        warning=(
            "This run is a plastic / fly-ash composite side project. "
            "Keep its data separate from the main fly-ash experiment."
        ),
    ),
}

# Run types whose data file is the canonical measured-release CSV. Only these may
# be written through the lab path or exported into the pipeline.
LAB_LIKE_RUN_TYPES = [
    rt for rt, spec in RUN_TYPE_SPECS.items()
    if spec.data_filename == "experimental_release.csv"
]


class RunManagerError(Exception):
    """Base error for run-manager misuse (unknown run, wrong type, …)."""


class RunTypeError(RunManagerError):
    """Raised when an operation is attempted against an incompatible run_type.

    This is the guardrail that stops, e.g., literature data being written into a
    lab run's experimental_release.csv.
    """


# --------------------------------------------------------------------------- #
# Tiny YAML (flat scalar mapping only)
# --------------------------------------------------------------------------- #
def _dump_yaml(mapping: dict) -> str:
    """Serialise a flat ``{str: scalar}`` mapping to YAML.

    Every value is emitted as a JSON double-quoted scalar, which is also valid
    YAML and safely escapes colons, newlines and quotes — no PyYAML needed.
    """
    lines = []
    for key, value in mapping.items():
        text = "" if value is None else str(value)
        lines.append(f"{key}: {json.dumps(text)}")
    return "\n".join(lines) + "\n"


def _load_yaml(text: str) -> dict:
    """Parse the flat YAML produced by :func:`_dump_yaml` back to a dict."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value == "":
            out[key] = ""
            continue
        try:
            out[key] = json.loads(value)  # quoted scalar -> python str
        except (ValueError, json.JSONDecodeError):
            out[key] = value.strip().strip('"')
    return out


# --------------------------------------------------------------------------- #
# Naming + paths
# --------------------------------------------------------------------------- #
def safe_run_name(name: str) -> str:
    """Turn a free-text run name into a filesystem-safe folder name.

    Lower-cases, replaces any run of non-alphanumeric characters with a single
    underscore, and trims leading/trailing underscores. Empty / all-symbol input
    raises rather than producing an unnamed folder.
    """
    if name is None:
        raise RunManagerError("run name must not be None")
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower()).strip("_")
    if not slug:
        raise RunManagerError(f"run name {name!r} has no usable characters")
    return slug


def runs_root() -> Path:
    return config.EXPERIMENT_RUNS_DIR


def run_dir(run_name: str) -> Path:
    """Folder for a run (by raw or already-safe name)."""
    return runs_root() / safe_run_name(run_name)


def run_config_path(run_name: str) -> Path:
    return run_dir(run_name) / RUN_CONFIG_FILENAME


def run_data_dir(run_name: str) -> Path:
    return run_dir(run_name) / "data"


def run_outputs_dir(run_name: str) -> Path:
    return run_dir(run_name) / "outputs"


def run_exists(run_name: str) -> bool:
    return run_config_path(run_name).exists()


def _validate_run_type(run_type: str) -> None:
    if run_type not in RUN_TYPE_SPECS:
        raise RunManagerError(
            f"unknown run_type {run_type!r}; allowed: {', '.join(RUN_TYPES)}"
        )


def spec_for(run_type: str) -> RunTypeSpec:
    _validate_run_type(run_type)
    return RUN_TYPE_SPECS[run_type]


def warning_for(run_type: str) -> str:
    return spec_for(run_type).warning


def columns_for(run_type: str) -> list[str]:
    return list(spec_for(run_type).columns)


# --------------------------------------------------------------------------- #
# Create / load runs
# --------------------------------------------------------------------------- #
def create_run(
    run_name: str,
    run_type: str,
    *,
    description: str = "",
    notes: str = "",
    data_source: str | None = None,
    created_at: str | None = None,
    exist_ok: bool = False,
) -> Path:
    """Create a run folder with ``run_config.yaml``, ``data/`` and ``outputs/``.

    ``data_source`` defaults to the canonical source for the run_type. Returns the
    run directory. Raises :class:`RunManagerError` on an unknown run_type or if the
    run already exists (unless ``exist_ok``).
    """
    _validate_run_type(run_type)
    spec = RUN_TYPE_SPECS[run_type]
    if data_source is None:
        data_source = spec.data_source
    elif data_source not in DATA_SOURCES:
        raise RunManagerError(
            f"unknown data_source {data_source!r}; allowed: {', '.join(DATA_SOURCES)}"
        )

    directory = run_dir(run_name)
    if run_exists(run_name) and not exist_ok:
        raise RunManagerError(f"run already exists: {directory}")

    run_data_dir(run_name).mkdir(parents=True, exist_ok=True)
    run_outputs_dir(run_name).mkdir(parents=True, exist_ok=True)

    if created_at is None:
        created_at = datetime.now().isoformat(timespec="seconds")

    run_config = {
        "run_name": run_name,
        "run_type": run_type,
        "created_at": created_at,
        "description": description,
        "data_source": data_source,
        "notes": notes,
    }
    run_config_path(run_name).write_text(_dump_yaml(run_config), encoding="utf-8")
    return directory


def load_run_config(run_name: str) -> dict:
    path = run_config_path(run_name)
    if not path.exists():
        raise RunManagerError(f"no run_config.yaml for run {run_name!r} at {path}")
    return _load_yaml(path.read_text(encoding="utf-8"))


def list_runs() -> list[str]:
    """Sorted safe-names of every run under the runs root (those with a config)."""
    root = runs_root()
    if not root.exists():
        return []
    names = [
        p.name for p in root.iterdir()
        if p.is_dir() and (p / RUN_CONFIG_FILENAME).exists()
    ]
    return sorted(names)


# --------------------------------------------------------------------------- #
# Typed data paths (the guardrails)
# --------------------------------------------------------------------------- #
def require_run_type(run_name: str, allowed: list[str]) -> dict:
    """Load a run's config and assert its run_type is in ``allowed``.

    Returns the loaded config so callers can reuse it. Raises
    :class:`RunTypeError` on a mismatch — this is what keeps, e.g., literature
    data out of a lab experimental file.
    """
    cfg = load_run_config(run_name)
    rt = cfg.get("run_type")
    if rt not in allowed:
        raise RunTypeError(
            f"run {run_name!r} is run_type {rt!r}; this operation requires one of "
            f"{allowed}."
        )
    return cfg


def data_file_path(run_name: str) -> Path:
    """Path to the run's data CSV, chosen by its run_type (no type restriction)."""
    cfg = load_run_config(run_name)
    spec = RUN_TYPE_SPECS[cfg["run_type"]]
    return run_data_dir(run_name) / spec.data_filename


def lab_release_path(run_name: str) -> Path:
    """Path to a lab-type run's ``experimental_release.csv``.

    Raises :class:`RunTypeError` for non-lab runs, so literature/synthetic data can
    never be written here.
    """
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    return run_data_dir(run_name) / "experimental_release.csv"


def literature_path(run_name: str) -> Path:
    """Path to a literature run's ``literature_benchmark.csv`` (literature only)."""
    require_run_type(run_name, ["literature_benchmark"])
    return run_data_dir(run_name) / "literature_benchmark.csv"


def demo_path(run_name: str) -> Path:
    """Path to a synthetic_demo run's ``demo_data.csv`` (synthetic only)."""
    require_run_type(run_name, ["synthetic_demo"])
    return run_data_dir(run_name) / "demo_data.csv"


# --------------------------------------------------------------------------- #
# Read / append rows
# --------------------------------------------------------------------------- #
def read_data_file(run_name: str) -> pd.DataFrame:
    """Read a run's data CSV (empty frame with the right header if not created)."""
    cfg = load_run_config(run_name)
    spec = RUN_TYPE_SPECS[cfg["run_type"]]
    path = run_data_dir(run_name) / spec.data_filename
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame(columns=spec.columns)


def _append_row(path: Path, row: dict, columns: list[str]) -> Path:
    """Append one aligned row to a CSV, writing the header on first write."""
    aligned = {col: ("" if row.get(col) is None else row.get(col)) for col in columns}
    frame = pd.DataFrame([aligned], columns=columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    frame.to_csv(path, mode="a", header=write_header, index=False)
    return path


def append_lab_row(run_name: str, row: dict) -> Path:
    """Append a measured-release row to a lab-type run (pH-only or full ICP).

    Blank chemistry fields are allowed. Raises :class:`RunTypeError` for non-lab
    runs.
    """
    path = lab_release_path(run_name)
    return _append_row(path, row, config.EXPERIMENTAL_RELEASE_COLUMNS)


def append_literature_row(run_name: str, row: dict) -> Path:
    """Append a row to a literature run's benchmark file (literature only)."""
    path = literature_path(run_name)
    return _append_row(path, row, LITERATURE_BENCHMARK_COLUMNS)


def append_demo_row(run_name: str, row: dict) -> Path:
    """Append a synthetic_demo row, forcing ``source_type=synthetic_demo``."""
    path = demo_path(run_name)
    stamped = dict(row)
    stamped["source_type"] = SYNTHETIC_SOURCE_TAG
    return _append_row(path, stamped, DEMO_DATA_COLUMNS)


def save_literature_dataframe(run_name: str, df: pd.DataFrame) -> Path:
    """Overwrite a literature run's benchmark CSV from an uploaded DataFrame.

    Reindexes to the literature schema (extra columns kept after the canonical
    ones) so an uploaded file always lands with the expected headers first.
    """
    path = literature_path(run_name)
    extra = [c for c in df.columns if c not in LITERATURE_BENCHMARK_COLUMNS]
    ordered = LITERATURE_BENCHMARK_COLUMNS + extra
    out = df.reindex(columns=ordered)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------- #
# Pipeline bridge
# --------------------------------------------------------------------------- #
def export_lab_run_to_pipeline(run_name: str) -> Path:
    """Copy a lab run's ``experimental_release.csv`` into the existing pipeline.

    Writes to ``data/raw/experimental_icp/experimental_release_manual_entry.csv``
    (the file scripts 05/07 already read) so the existing scripts run unchanged.
    Raises :class:`RunTypeError` for non-lab runs and :class:`RunManagerError` if
    the run has no data file yet.
    """
    src = lab_release_path(run_name)  # also enforces run_type
    if not src.exists():
        raise RunManagerError(
            f"run {run_name!r} has no experimental_release.csv yet — enter data first."
        )
    dest = config.EXPERIMENTAL_ICP_DIR / "experimental_release_manual_entry.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    pd.read_csv(src).to_csv(dest, index=False)
    return dest
