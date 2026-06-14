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

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config, units

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
    id_column: str  # the row's human-facing identifier (used for blank detection)


# Per-run-type behaviour, in one place so the app and the module agree.
RUN_TYPE_SPECS: dict[str, RunTypeSpec] = {
    "lab_experiment": RunTypeSpec(
        data_source="experimental",
        data_filename="experimental_release.csv",
        columns=list(config.EXPERIMENTAL_RELEASE_COLUMNS),
        warning="This run contains real measured lab data.",
        id_column="sample_id",
    ),
    "literature_benchmark": RunTypeSpec(
        data_source="literature",
        data_filename="literature_benchmark.csv",
        columns=list(LITERATURE_BENCHMARK_COLUMNS),
        warning=(
            "This run contains literature data only. "
            "Do not treat this as our measured experiment."
        ),
        id_column="source_id",
    ),
    "synthetic_demo": RunTypeSpec(
        data_source="synthetic",
        data_filename="demo_data.csv",
        columns=list(DEMO_DATA_COLUMNS),
        warning=(
            "This run contains synthetic/demo data only. "
            "It is for testing code, not scientific conclusions."
        ),
        id_column="sample_id",
    ),
    "plastic_composite": RunTypeSpec(
        data_source="experimental",
        data_filename="experimental_release.csv",
        columns=list(config.EXPERIMENTAL_RELEASE_COLUMNS),
        warning=(
            "This run is a plastic / fly-ash composite side project. "
            "Keep its data separate from the main fly-ash experiment."
        ),
        id_column="sample_id",
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


def generated_simulations_dir(run_name: str) -> Path:
    """Where this run's generated PHREEQC `.pqi`/`.pqo` files live (gitignored).

    Real generated runs go under ``experiments/<run>/outputs/generated/``, never
    ``data/raw/`` (which is for hand-built, committed inputs).
    """
    path = run_outputs_dir(run_name) / "generated"
    path.mkdir(parents=True, exist_ok=True)
    return path


def surrogate_dir(run_name: str) -> Path:
    """Where this run's surrogate design/dataset/models live (gitignored)."""
    path = run_outputs_dir(run_name) / "surrogate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def residual_model_dir(run_name: str) -> Path:
    """Where this run's trained residual-correction models + cards live (gitignored)."""
    path = run_outputs_dir(run_name) / "residual_model"
    path.mkdir(parents=True, exist_ok=True)
    return path


def incompleteness_model_dir(run_name: str) -> Path:
    """Where this run's trained model-incompleteness GPs + cards live (gitignored)."""
    path = run_outputs_dir(run_name) / "incompleteness_model"
    path.mkdir(parents=True, exist_ok=True)
    return path


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


# Columns an uploaded lab CSV must contain (the rest of the release schema is
# optional). These are the experiment metadata + pH that every measured row needs;
# chemistry/ICP columns may be filled in later.
LAB_REQUIRED_COLUMNS = [
    "sample_id",
    "experiment_date",
    "fly_ash_type",
    "NaOH_M",
    "time_min",
    "temperature_C",
    "liquid_solid_ratio",
    "CO2_condition",
    "initial_pH",
    "final_pH",
]
LAB_OPTIONAL_COLUMNS = [
    c for c in config.EXPERIMENTAL_RELEASE_COLUMNS if c not in LAB_REQUIRED_COLUMNS
]


def missing_lab_required_columns(df: pd.DataFrame) -> list[str]:
    """Return the required lab columns absent from ``df`` (empty list = valid)."""
    return [c for c in LAB_REQUIRED_COLUMNS if c not in df.columns]


def save_lab_dataframe(run_name: str, df: pd.DataFrame, mode: str = "replace",
                       *, audit_context: dict | None = None) -> Path:
    """Write an uploaded DataFrame to a lab-type run's ``experimental_release.csv``.

    ``mode="replace"`` overwrites the run's CSV; ``mode="append"`` concatenates the
    new rows onto any existing ones. The frame is reindexed to the release schema
    (extra columns kept after the canonical ones). Raises :class:`RunTypeError` for
    non-lab runs, so literature/synthetic data can never land in a lab release file.

    Appends one ``import`` event to the run's audit log (rows + mode + column names +
    conversion ids — never any value). ``audit_context`` lets the caller add the file
    name / sheet / column mapping it knows (the app does); omit it (the test does) and
    a still-valid import event is logged.
    """
    if mode not in ("replace", "append"):
        raise ValueError(f"mode must be 'replace' or 'append', got {mode!r}")
    path = lab_release_path(run_name)  # enforces the lab-only guardrail
    extra = [c for c in df.columns if c not in config.EXPERIMENTAL_RELEASE_COLUMNS]
    ordered = list(config.EXPERIMENTAL_RELEASE_COLUMNS) + extra
    out = df.reindex(columns=ordered)
    if mode == "append" and path.exists():
        existing = pd.read_csv(path)
        cols = ordered + [c for c in existing.columns if c not in ordered]
        out = pd.concat(
            [existing.reindex(columns=cols), out.reindex(columns=cols)],
            ignore_index=True,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)

    from . import audit  # lazy: audit imports run_manager (no module-level cycle)
    audit.log_import(run_name, n_rows=int(len(df)), mode=mode,
                     columns=[c for c in df.columns
                              if not units.is_conversion_provenance_column(c)],
                     conversions=audit.conversions_from_frame(df),
                     **(audit_context or {}))
    return path


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


def id_column_for(run_name: str) -> str:
    """The run's identifier column (``sample_id`` or ``source_id``)."""
    cfg = load_run_config(run_name)
    return RUN_TYPE_SPECS[cfg["run_type"]].id_column


def save_data_file(run_name: str, df: pd.DataFrame) -> Path:
    """Write a DataFrame back to the run's own data CSV (run-type-aware path).

    Touches only this run's file under ``experiments/<run>/data/`` — never another
    run and never ``data/raw/experimental_icp``.
    """
    path = data_file_path(run_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


def _is_blank(value) -> bool:
    """A cell counts as blank if it is NaN/None or empty after stripping."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):  # pragma: no cover - non-scalar guard
        pass
    return str(value).strip() == ""


def delete_data_rows(run_name: str, row_indices) -> int:
    """Delete rows by 0-based position from the run's data CSV.

    Only the rows at the given positions are removed; the file itself is kept (an
    empty file with headers remains if all rows are removed). Out-of-range and
    duplicate indices are ignored. Returns the number of rows actually deleted.
    Works for any run type (lab / literature / synthetic / plastic).
    """
    path = data_file_path(run_name)
    if not path.exists():
        return 0
    df = read_data_file(run_name)
    n = len(df)
    valid = sorted({int(i) for i in row_indices if 0 <= int(i) < n})
    if not valid:
        return 0
    kept = df.drop(df.index[valid]).reset_index(drop=True)
    save_data_file(run_name, kept)
    return len(valid)


def remove_blank_data_rows(run_name: str) -> int:
    """Remove rows whose id column is blank, or where every cell is blank.

    Returns the number of blank rows removed. Works for any run type. The file is
    preserved (kept rows are written back); if there are no blank rows the file is
    left untouched.
    """
    path = data_file_path(run_name)
    if not path.exists():
        return 0
    df = read_data_file(run_name)
    if df.empty:
        return 0

    all_blank = df.apply(lambda row: all(_is_blank(v) for v in row), axis=1)
    id_col = id_column_for(run_name)
    if id_col in df.columns:
        blank_id = df[id_col].apply(_is_blank)
        mask = all_blank | blank_id
    else:
        mask = all_blank

    n_blank = int(mask.sum())
    if n_blank:
        kept = df[~mask].reset_index(drop=True)
        save_data_file(run_name, kept)
    return n_blank


# --------------------------------------------------------------------------- #
# Sample -> PHREEQC mapping
# --------------------------------------------------------------------------- #
# The comparison script (scripts/05) reads this 2-column file to link each
# measured sample_id to a PHREEQC record_key. Same filename/columns as the
# pipeline expects (config.SAMPLE_PHREEQC_MAP_CSV).
MAPPING_COLUMNS = ["sample_id", "phreeqc_record_key"]


def mapping_path(run_name: str) -> Path:
    """Path to a lab-type run's ``sample_phreeqc_map.csv`` (lab-like runs only)."""
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    return run_data_dir(run_name) / config.SAMPLE_PHREEQC_MAP_CSV


def read_mapping(run_name: str) -> pd.DataFrame:
    """Read the run's sample->PHREEQC mapping (empty 2-col frame if not created)."""
    path = mapping_path(run_name)
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame(columns=MAPPING_COLUMNS)


def summarize_mapping(mapping: pd.DataFrame) -> dict:
    """Summarise a sample->PHREEQC mapping for quality checks (pure, no I/O).

    Returns a dict with:
    * ``n_samples`` — number of non-blank mapped samples,
    * ``n_unique_rows`` — number of distinct PHREEQC ``record_key`` values used,
    * ``samples_per_row`` — ``{record_key: count}``, descending by count,
    * ``duplicated_rows`` — record_keys mapped by more than one sample,
    * ``has_collisions`` — True if any PHREEQC row is shared by 2+ samples.

    A collision means several samples point at the *same* model prediction, which
    is what makes a scatter plot collapse to a vertical line.
    """
    if mapping is None or mapping.empty or "phreeqc_record_key" not in mapping.columns:
        return {
            "n_samples": 0, "n_unique_rows": 0,
            "samples_per_row": {}, "duplicated_rows": [], "has_collisions": False,
        }
    keys = mapping["phreeqc_record_key"].astype(str).str.strip()
    keys = keys[(keys != "") & (keys.str.lower() != "nan")]
    counts = keys.value_counts()
    samples_per_row = {k: int(v) for k, v in counts.items()}
    duplicated = [k for k, v in samples_per_row.items() if v > 1]
    return {
        "n_samples": int(keys.shape[0]),
        "n_unique_rows": int(counts.shape[0]),
        "samples_per_row": samples_per_row,
        "duplicated_rows": duplicated,
        "has_collisions": bool(duplicated),
    }


def add_mapping(run_name: str, sample_id: str, phreeqc_record_key: str) -> pd.DataFrame:
    """Upsert one ``sample_id -> phreeqc_record_key`` link and save it.

    If ``sample_id`` is already mapped, its row is replaced (one link per sample).
    Returns the full mapping frame after the write.
    """
    sid = str(sample_id).strip()
    key = str(phreeqc_record_key).strip()
    if not sid:
        raise RunManagerError("sample_id must not be blank")
    if not key:
        raise RunManagerError("phreeqc_record_key must not be blank")

    df = read_mapping(run_name)
    if "sample_id" in df.columns and not df.empty:
        df = df[df["sample_id"].astype(str).str.strip() != sid]
    new = pd.DataFrame([{"sample_id": sid, "phreeqc_record_key": key}], columns=MAPPING_COLUMNS)
    out = pd.concat([df, new], ignore_index=True)[MAPPING_COLUMNS]

    path = mapping_path(run_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return out


def delete_mapping_rows(run_name: str, row_indices) -> int:
    """Delete mapping rows by 0-based position. Returns the number removed."""
    path = mapping_path(run_name)
    if not path.exists():
        return 0
    df = read_mapping(run_name)
    n = len(df)
    valid = sorted({int(i) for i in row_indices if 0 <= int(i) < n})
    if not valid:
        return 0
    kept = df.drop(df.index[valid]).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept.to_csv(path, index=False)
    return len(valid)


def has_mapping(run_name: str) -> bool:
    """True if the run has at least one non-blank sample->PHREEQC link."""
    df = read_mapping(run_name)
    if df.empty or "sample_id" not in df.columns:
        return False
    return bool(df["sample_id"].apply(lambda v: not _is_blank(v)).any())


def export_mapping_to_pipeline(run_name: str) -> Path:
    """Copy the run's mapping into the location the comparison script reads.

    Writes to ``data/raw/experimental_icp/sample_phreeqc_map.csv`` so step 05 picks
    it up automatically. Raises if the run has no mapping yet.
    """
    src = mapping_path(run_name)  # enforces lab-like run_type
    if not src.exists():
        raise RunManagerError(
            f"run {run_name!r} has no sample->PHREEQC mapping yet — create one first."
        )
    dest = config.EXPERIMENTAL_ICP_DIR / config.SAMPLE_PHREEQC_MAP_CSV
    dest.parent.mkdir(parents=True, exist_ok=True)
    pd.read_csv(src).to_csv(dest, index=False)
    return dest


# --------------------------------------------------------------------------- #
# Condition-level mapping (replicate-aware) — condition_key -> PHREEQC record_key
# --------------------------------------------------------------------------- #
# Maps one experimental condition (all its replicate batches) to a PHREEQC
# scenario; expanded to the per-sample mapping the pipeline already reads.
CONDITION_MAPPING_COLUMNS = ["condition_key", "phreeqc_record_key", "notes", "override"]
CONDITION_MAPPING_FILENAME = "condition_phreeqc_map.csv"
# Optional advanced map: which PHREEQC solution number each replicate id uses.
REPLICATE_SOLUTION_COLUMNS = ["replicate_id", "solution_number"]
REPLICATE_SOLUTION_FILENAME = "replicate_solution_map.csv"


def condition_mapping_path(run_name: str) -> Path:
    """Path to a lab-like run's condition→PHREEQC map (lab-like runs only)."""
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    return run_data_dir(run_name) / CONDITION_MAPPING_FILENAME


def read_condition_mapping(run_name: str) -> pd.DataFrame:
    """Read the run's condition→PHREEQC map (empty frame if not created).

    Back-compat: older files may lack the optional ``notes`` / ``override`` columns,
    which are added (blank / ``False``) on read so callers always see the full schema.
    """
    path = condition_mapping_path(run_name)
    if path.exists():
        df = pd.read_csv(path)
        if "notes" not in df.columns:
            df["notes"] = ""
        if "override" not in df.columns:
            df["override"] = False
        return df
    return pd.DataFrame(columns=CONDITION_MAPPING_COLUMNS)


def add_condition_mapping(run_name: str, condition_key: str, phreeqc_record_key: str,
                          notes: str = "", override: bool = False,
                          mapping_status: str | None = None) -> pd.DataFrame:
    """Upsert one ``condition_key -> phreeqc_record_key`` link (+ optional notes).

    ``notes`` is free-text validation context (CO2/cover caveats, who mapped it,
    etc.). ``override=True`` records that a human deliberately saved a mapping the
    suggestion table would otherwise refuse (e.g. an *unsafe* mapping confirmed via
    the Advanced manual-override path). Both columns live only in the condition map —
    they never reach the per-sample map the comparison step reads, so the pipeline is
    unaffected.
    """
    ck = str(condition_key).strip()
    key = str(phreeqc_record_key).strip()
    if not ck:
        raise RunManagerError("condition_key must not be blank")
    if not key:
        raise RunManagerError("phreeqc_record_key must not be blank")
    df = read_condition_mapping(run_name)
    if "condition_key" in df.columns and not df.empty:
        df = df[df["condition_key"].astype(str).str.strip() != ck]
    new = pd.DataFrame([{"condition_key": ck, "phreeqc_record_key": key,
                         "notes": str(notes or "").strip(), "override": bool(override)}],
                       columns=CONDITION_MAPPING_COLUMNS)
    out = pd.concat([df, new], ignore_index=True)[CONDITION_MAPPING_COLUMNS]
    path = condition_mapping_path(run_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)

    from . import audit  # lazy import (no module-level cycle)
    audit.log_mapping_accepted(run_name, condition_key=ck, phreeqc_record_key=key,
                               mapping_status=mapping_status, override=bool(override),
                               has_notes=bool(str(notes or "").strip()))
    return out


def delete_condition_mapping_rows(run_name: str, row_indices) -> int:
    """Delete condition-mapping rows by 0-based position. Returns the number removed."""
    path = condition_mapping_path(run_name)
    if not path.exists():
        return 0
    df = read_condition_mapping(run_name)
    valid = sorted({int(i) for i in row_indices if 0 <= int(i) < len(df)})
    if not valid:
        return 0
    removed_keys = (df.iloc[valid]["condition_key"].astype(str).tolist()
                    if "condition_key" in df.columns else [])
    kept = df.drop(df.index[valid]).reset_index(drop=True)
    kept.to_csv(path, index=False)

    from . import audit  # lazy import (no module-level cycle)
    audit.log_mapping_deleted(run_name, scope="condition", n_deleted=len(valid),
                              keys=removed_keys)
    return len(valid)


def has_condition_mapping(run_name: str) -> bool:
    """True if the run has at least one non-blank condition→PHREEQC link."""
    df = read_condition_mapping(run_name)
    if df.empty or "condition_key" not in df.columns:
        return False
    return bool(df["condition_key"].apply(lambda v: not _is_blank(v)).any())


def read_replicate_solution_map(run_name: str) -> pd.DataFrame:
    """Read the optional replicate_id→solution_number map (empty frame if none)."""
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    path = run_data_dir(run_name) / REPLICATE_SOLUTION_FILENAME
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame(columns=REPLICATE_SOLUTION_COLUMNS)


def add_replicate_solution(run_name: str, replicate_id: str, solution_number: str) -> pd.DataFrame:
    """Upsert one ``replicate_id -> solution_number`` link (advanced, optional)."""
    rid = str(replicate_id).strip().upper()
    sol = str(solution_number).strip()
    if not rid or not sol:
        raise RunManagerError("replicate_id and solution_number must not be blank")
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    df = read_replicate_solution_map(run_name)
    if "replicate_id" in df.columns and not df.empty:
        df = df[df["replicate_id"].astype(str).str.strip().str.upper() != rid]
    new = pd.DataFrame([{"replicate_id": rid, "solution_number": sol}],
                       columns=REPLICATE_SOLUTION_COLUMNS)
    out = pd.concat([df, new], ignore_index=True)[REPLICATE_SOLUTION_COLUMNS]
    path = run_data_dir(run_name) / REPLICATE_SOLUTION_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return out


def apply_condition_mapping(run_name: str, *, use_replicate_solution: bool = False) -> Path:
    """Expand the condition→PHREEQC map to the per-sample map the pipeline reads.

    Every replicate row inherits its condition's mapping (or, when
    ``use_replicate_solution`` and a replicate→solution map exists, each replicate
    points at its own PHREEQC solution number). Writes the run's
    ``sample_phreeqc_map.csv`` so :func:`export_mapping_to_pipeline` + step 05 work
    unchanged. Raises if the run has no condition mapping yet.
    """
    from . import replicates  # local import keeps module import order simple

    if not has_condition_mapping(run_name):
        raise RunManagerError(
            f"run {run_name!r} has no condition→PHREEQC mapping yet — add one first."
        )
    data = read_data_file(run_name)
    cmap = read_condition_mapping(run_name)
    if use_replicate_solution:
        rs = read_replicate_solution_map(run_name)
        rs_dict = {str(r["replicate_id"]).strip().upper(): r["solution_number"]
                   for _, r in rs.iterrows()} if not rs.empty else {}
        sample_map = replicates.expand_replicate_solution_mapping(data, cmap, rs_dict)
    else:
        sample_map = replicates.expand_condition_mapping(data, cmap)

    path = mapping_path(run_name)  # the per-sample map step 05 reads
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_map.to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------- #
# Per-run comparison artifacts + provenance stamp
# --------------------------------------------------------------------------- #
# A lab run's measured-vs-PHREEQC comparison (CSV + figures) belongs to that run,
# under experiments/<run>/outputs/, so run B's Results tab can never display run
# A's comparison. A small JSON stamp records the content hashes of the three
# inputs the comparison was built from, so the app can flag stale results.
COMPARISON_FILENAME = config.COMPARISON_CSV  # comparison_measured_vs_phreeqc.csv
COMPARISON_META_FILENAME = "comparison_meta.json"
COMPARISON_FIGURES_DIRNAME = "figures"
# Per-element/per-condition systematic-bias table, derived from the comparison +
# the inclusion status join. It lives beside the comparison so the comparison's
# provenance stamp (which fingerprints the *inputs* both are built from) already
# covers its staleness — re-running on changed data/mapping/PHREEQC invalidates both.
BIAS_TABLE_FILENAME = "systematic_bias.csv"

# Human labels for the three provenance sources (used in stale-reason messages).
_COMPARISON_SOURCE_LABELS = {
    "data": "run data CSV",
    "mapping": "sample→PHREEQC mapping",
    "phreeqc_results": "PHREEQC results (data/processed/phreeqc_results.csv)",
}


def comparison_path(run_name: str) -> Path:
    """Path to a lab-like run's per-run comparison CSV (lab-like runs only)."""
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    return run_outputs_dir(run_name) / COMPARISON_FILENAME


def comparison_figures_dir(run_name: str) -> Path:
    """Directory for a lab-like run's per-run comparison figures (lab-like only)."""
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    return run_outputs_dir(run_name) / COMPARISON_FIGURES_DIRNAME


def comparison_meta_path(run_name: str) -> Path:
    """Path to a lab-like run's comparison provenance stamp (lab-like runs only)."""
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    return run_outputs_dir(run_name) / COMPARISON_META_FILENAME


def bias_table_path(run_name: str) -> Path:
    """Path to a lab-like run's systematic-bias table (lab-like runs only).

    Written alongside the comparison; its staleness is covered by the comparison's
    provenance stamp because both derive from the same three inputs.
    """
    require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    return run_outputs_dir(run_name) / BIAS_TABLE_FILENAME


def has_comparison(run_name: str) -> bool:
    """True if this run has a generated per-run comparison CSV."""
    return comparison_path(run_name).exists()


def _file_fingerprint(path: Path) -> dict | None:
    """Content fingerprint (sha256 + byte size) of a file, or None if absent."""
    if not path.exists():
        return None
    raw = path.read_bytes()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def _comparison_source_paths(run_name: str) -> dict[str, Path]:
    """The three inputs a comparison is built from, by provenance key.

    The run's own data CSV and mapping (so editing either makes results stale) and
    the shared Phase-1 PHREEQC results (so re-parsing the model invalidates them).
    """
    return {
        "data": data_file_path(run_name),
        "mapping": mapping_path(run_name),
        "phreeqc_results": config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV,
    }


def write_comparison_meta(run_name: str, *, timestamp: str | None = None) -> Path:
    """Stamp the provenance of a just-generated comparison for this run.

    Records the run name/type, a timestamp, and content fingerprints of the run's
    data CSV, its ``sample_phreeqc_map.csv``, and the shared
    ``data/processed/phreeqc_results.csv``. :func:`comparison_is_current` re-checks
    these to decide whether the stored results still match the live inputs.
    """
    cfg = require_run_type(run_name, LAB_LIKE_RUN_TYPES)
    if timestamp is None:
        timestamp = datetime.now().isoformat(timespec="seconds")
    sources = {
        key: _file_fingerprint(path)
        for key, path in _comparison_source_paths(run_name).items()
    }
    meta = {
        "run_name": run_name,
        "run_type": cfg.get("run_type"),
        "generated_at": timestamp,
        "sources": sources,
    }
    path = comparison_meta_path(run_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def read_comparison_meta(run_name: str) -> dict | None:
    """Read the comparison provenance stamp, or None if absent/unparseable."""
    path = comparison_meta_path(run_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError):  # pragma: no cover - corrupt stamp
        return None


def comparison_is_current(run_name: str) -> tuple[bool, list[str]]:
    """Is the stored comparison still consistent with its inputs?

    Returns ``(is_current, stale_reasons)``. ``is_current`` is True only when a
    comparison CSV + provenance stamp exist and every source file's fingerprint
    still matches what was recorded. Otherwise ``stale_reasons`` explains why (no
    results yet, no stamp, or which input changed/appeared/disappeared).
    """
    if not comparison_path(run_name).exists():
        return False, ["No comparison has been generated for this run yet."]
    meta = read_comparison_meta(run_name)
    if meta is None:
        return False, ["No provenance stamp found for this run's comparison."]

    stored = meta.get("sources", {})
    reasons: list[str] = []
    for key, path in _comparison_source_paths(run_name).items():
        label = _COMPARISON_SOURCE_LABELS[key]
        live = _file_fingerprint(path)
        recorded = stored.get(key)
        if live is None and recorded is None:
            continue
        if recorded is None and live is not None:
            reasons.append(f"{label} now exists but was absent when results were generated.")
        elif recorded is not None and live is None:
            reasons.append(f"{label} is missing now but was present when results were generated.")
        elif live != recorded:
            reasons.append(f"{label} changed since results were generated.")
    return (not reasons), reasons


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
