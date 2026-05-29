# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

WPI **Class C fly ash + PHREEQC** geochemical modelling project. It combines PHREEQC
speciation/equilibrium simulations of high-pH (~13) Na–Si–Al–Ca alkali-activated systems
(CEMDATA18 database, CO₂ carbonation) with experimental ICP measurements, working toward
predicting measured fly-ash outcomes (Ca/Si/Al/Fe/REE/Sc release, pH, carbonate formation).

The long-term aim is an ML *correction* layer that learns where PHREEQC disagrees with
experiment — **not** a blind replacement for the chemistry.

### Completed phases

- **Phase 1 — parse + analyze.** Parsers for `.pqi`, `.pqo`, `SELECTED_OUTPUT`, and the ICP
  workbook produce clean processed CSVs and `master_dataset.csv`, plus exploratory plots.
- **Phase 2 — PHREEQC vs experiment.** A measured-experimental-release template + parser, and
  a residual comparison (`measured − PHREEQC` for Ca/Si/Al/Fe/pH) with measured-vs-PHREEQC plots.
  The machinery is in place and tested but **dormant until measured data exists**.

Phase 3 (ML) is not started.

## Working rules (project-specific)

- **No ML training yet.** Do not build/train models unless measured experimental release data
  actually exists in `data/raw/experimental_icp/` (a filled CSV, not just the blank template).
  Until then, Phase 2 comparison is the ceiling.
- **Generated artifacts are not committed** unless explicitly requested. `data/processed/*.csv`
  and `reports/figures/*.png` are gitignored and re-creatable by running the scripts.
- **Confidential raw research data:** do not commit raw research data unless the user confirms
  it is allowed. `data/raw/` is currently tracked, so be deliberate about anything added there.
- **Run `pytest` before committing** any code change, and keep code modular, simple, and tested.

## Commands

```bash
# setup (virtualenv lives at .venv/)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # runtime: pandas/numpy/matplotlib/openpyxl
pip install -r requirements-dev.txt    # pytest

# main pipelines
python scripts/run_phase1.py            # Phase 1: parse -> processed CSVs -> master_dataset -> plots
python scripts/05_compare_experimental.py  # Phase 2: measured vs PHREEQC (no-op until data exists)

# tests
python -m pytest                        # full suite
python -m pytest tests/test_comparison.py::test_residuals_match_spec   # a single test
```

Scripts self-bootstrap `sys.path` via `scripts/_path_setup.py` / `conftest.py`, so they run
without `pip install -e .` (which is optional). If `pytest` isn't found, use `python -m pytest`
inside the venv.

## Architecture

The package `flyash_phreeqc_ml/` is split by concern; `scripts/` are thin entry points that wire
modules together and own all file I/O paths.

- **`config.py` is the single source of truth.** All filesystem paths, the experimental-release
  CSV **schema** (`EXPERIMENTAL_RELEASE_COLUMNS` / `EXPERIMENTAL_NUMERIC_COLUMNS`), key elements/
  phases, and the molality→mM factor live here. The shipped template CSV, the parser, and the
  tests all derive from this list — change the schema here, not in three places.

- **`parsers/`** turn raw files into tidy DataFrames:
  - `pqo_parser.py` is the core. PHREEQC `.pqo` output is verbose text; the parser walks it
    line-by-line tracking `(simulation, state, solution)` context, where `state` is `initial`
    (pre-reaction) or `batch` (post-equilibration). It parses four dashed-banner blocks —
    *Solution composition, Description of solution, Phase assemblage, Saturation indices* — into
    one wide `results` row per state plus long `saturation`/`assemblage` tables.
  - `pqi_parser.py` reads input solutions + equilibrium phases; `selected_output_parser.py`
    handles the cleaner `SELECTED_OUTPUT` tables when present.
  - `icp_parser.py` does double duty: best-effort extraction from the messy CFA+MK mix-design
    workbook, **and** the Phase-2 experimental ingestion (`parse_experimental_release`,
    `load_experimental_release`, `has_measured_data`).

- **`datasets/build_master.py`** joins each PHREEQC output state to its input composition on
  `solution_number` (output `.pqo` and input `.pqi` filenames differ, so the join is by number,
  first definition wins) → `master_dataset.csv`.

- **`compare/residuals.py`** (Phase 2) converts PHREEQC molality to mM and computes
  `residual_<X> = measured − PHREEQC`. Measured samples link to PHREEQC runs via an explicit
  `sample_id → record_key` mapping (`data/raw/experimental_icp/sample_phreeqc_map.csv`); with no
  mapping, predictions/residuals stay NaN rather than mis-joining (a deliberate, visible state).

- **`viz/`** — `plots.py` (Phase 1 exploratory) and `compare_plots.py` (Phase 2), the latter
  only emitting figures when measured/PHREEQC pairs exist.

### Key conventions

- A PHREEQC solution state is identified by `record_key` = `"<file>|sim<N>|<state>|sol<N>"`; this
  is the join key between PHREEQC results and measured samples.
- Comparisons default to PHREEQC `state == "batch"` (the post-equilibration result that an
  experiment measures).
- Phase 2 is built to be a no-op until data lands: `run_phase1.py` is untouched by Phase 2, and
  step 05 detects a blank template and exits cleanly. Keep this separation when extending.
