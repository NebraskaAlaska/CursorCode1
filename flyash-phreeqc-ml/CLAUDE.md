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
- **Experiment-planning + QA/QC tooling** (pre-data, no ML). `flyash_phreeqc_ml/experiments/`
  (the *package*) + scripts 06–08: generate the run sheet, validate a filled release CSV
  (error/warning report), and compute sustainability *proxy* indicators. These run before
  measured data exists and feed Phase 2.
- **Experiment Run Manager** (app-level "save files", no ML). `flyash_phreeqc_ml/run_manager.py`
  + the top-level `experiments/` *data folder*. Lets one app hold several independent runs
  (lab / literature / synthetic / plastic-composite), each in `experiments/<safe_name>/` with a
  `run_config.yaml`, `data/`, and `outputs/`. It is a save/open layer over the existing workflow,
  not a replacement, and it keeps literature data out of the measured-release file.
- **Calculation verification / formula audit** (transparency, no ML). `flyash_phreeqc_ml/calculations.py`
  + the app's **Calculation Verification** tab document every downstream formula (residuals, ICP
  mg/L→mM, dilution, L/S ratio, mass released, recovery) and **re-derive** the stored residuals to
  confirm they match (`pass`/`warning`/`fail`/`not available`). PHREEQC's SI and pH are explained,
  not recomputed.

Phase 3 (ML) is not started.

> **Two different `experiments/`.** `flyash_phreeqc_ml/experiments/` is the *Python package*
> (planning + QA/QC). The repo-root `experiments/` is the *data folder* of run save-files
> (gitignored except its `README.md`). Don't confuse them.

## Working rules (project-specific)

- **No ML training yet.** Do not build/train models unless measured experimental release data
  actually exists in `data/raw/experimental_icp/` (a filled CSV, not just the blank template).
  Until then, Phase 2 comparison is the ceiling.
- **Generated artifacts are not committed** unless explicitly requested. `data/processed/*.csv`,
  `reports/figures/*.png`, `outputs/tables/*.csv`, and the generated run sheet
  `data/raw/experimental_icp/experiment_plan.csv` are gitignored and re-creatable by
  running the scripts.
- **Confidential raw research data:** do not commit raw research data unless the user confirms
  it is allowed. `data/raw/` is currently tracked, so be deliberate about anything added there.
  The remote is confirmed **private**, and the existing `data/raw/` contents (UMass mix-design
  workbook, PHREEQC files) are approved to push there; re-confirm if the remote changes or any
  *new* raw dataset is added.
- **Measured release CSVs are gitignored by default.** `.gitignore` ignores `*release*.csv`,
  `20*_release*.csv`, `*measured*.csv`, the manual-entry file, the generated plan
  (`experiment_plan.csv`), and the generated `sample_phreeqc_map.csv` in
  `data/raw/experimental_icp/`, with a `!`-re-include keeping **only** the blank
  `experimental_release_template.csv` tracked. So real lab data stays out of git unless
  deliberately force-added. (gitignore comments must be on their own line — an inline `#` becomes
  part of the pattern.)
- **Experiment-run save-files are gitignored by default.** `.gitignore` ignores
  `experiments/*/data/*.csv`, `experiments/*/outputs/`, and `experiments/*/run_config.yaml`;
  **only** `experiments/README.md` is tracked. Run data (lab, literature, synthetic) and
  generated outputs stay out of git unless explicitly approved.
- **Run `pytest` before committing** any code change, and keep code modular, simple, and tested.

### Git layout (important)

This project is a **subdirectory inside a larger git repo** rooted at the parent directory
(`CursorCode1`), not its own repo. So `git status` run from here shows the parent repo, and
commits/pushes target it — stage paths as `flyash-phreeqc-ml/...`. Do **not** `git init` here
(it would create a confusing nested repo). When committing, prefer staging explicit files over a
blanket `git add` so untracked stray files (e.g. someone's half-named template copy) don't slip in.

## Commands

```bash
# setup (virtualenv lives at .venv/)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # runtime: pandas/numpy/matplotlib/openpyxl/streamlit
pip install -r requirements-dev.txt    # pytest

# main pipelines
python scripts/run_phase1.py            # Phase 1: parse -> processed CSVs -> master_dataset -> plots
python scripts/05_compare_experimental.py  # Phase 2: measured vs PHREEQC (no-op until data exists)

# experiment planning + QA/QC (pre-data; no ML)
python scripts/06_generate_experiment_plan.py    # -> data/raw/experimental_icp/experiment_plan.csv
python scripts/07_validate_experimental_data.py  # -> outputs/tables/experimental_validation_report.csv
python scripts/08_sustainability_score.py        # -> outputs/tables/sustainability_score.csv

# GUI (optional): thin Streamlit wrapper over the scripts above
streamlit run app.py

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

- **`experiments/`** (pre-data planning + QA/QC; no ML) — three independent helpers, all deriving
  their schema from `config`:
  - `plan_generator.py` expands four experiment sets (time / NaOH / CO₂ / replicate) into a run
    sheet, de-duplicating on the canonical `sample_id`
    (`CFA-NaOH{M}M-LS{ratio}-{min}min-{CO2}-R{rep}`). Plan columns match the release schema
    exactly (`fly_ash_type`), so the filled run sheet re-reads with the Phase-2 parser.
  - `validate_experimental_data.py` — `validate_experimental_df` returns a tidy issue report
    (severity `error`/`warning`/`ok`); `validate_experimental_dir` loops the measured CSVs.
  - `sustainability_score.py` — `compute_sustainability_scores` returns per-row **proxy**
    indicators (not real costs), NaN-safe on missing inputs.
  Outputs land in `outputs/tables/` (gitignored). The generated plan is added to
  `EXPERIMENTAL_NON_DATA_FILES` so the Phase-2 loader skips it.

- **`run_manager.py`** — the Experiment Run Manager (app-level "save files"). Pure file/IO,
  no chemistry. `RUN_TYPE_SPECS` maps each `run_type` (`lab_experiment`, `literature_benchmark`,
  `synthetic_demo`, `plastic_composite`) to its `data_source`, data filename, column schema, and
  warning. Typed path helpers (`lab_release_path` / `literature_path` / `demo_path`) call
  `require_run_type`, which **raises `RunTypeError`** on a mismatch — that guardrail is what keeps
  literature/synthetic data out of a lab run's `experimental_release.csv`. `create_run` writes a
  `run_config.yaml` (flat scalar mapping, serialized via a tiny built-in JSON-quoted-scalar YAML
  helper, so **no PyYAML dependency**). `export_lab_run_to_pipeline` copies a lab run's CSV into
  `data/raw/experimental_icp/experimental_release_manual_entry.csv` so the existing scripts run
  unchanged. Paths derive from `config.EXPERIMENT_RUNS_DIR`. Synthetic rows are force-tagged
  `source_type=synthetic_demo`. **Row editing:** `delete_data_rows` (by 0-based position) and
  `remove_blank_data_rows` clean a run's own CSV in place (file kept, only rows removed), for any
  run type. **Sample→PHREEQC mapping** (lab-like runs only): `add_mapping` upserts one
  `sample_id → phreeqc_record_key` link (columns `MAPPING_COLUMNS`, matching what `scripts/05` +
  `compare/residuals.py` expect), `read_mapping` / `delete_mapping_rows` / `has_mapping` manage it
  in `experiments/<run>/data/sample_phreeqc_map.csv`, and `export_mapping_to_pipeline` copies it to
  `data/raw/experimental_icp/sample_phreeqc_map.csv` for step 05.

- **`calculations.py`** — calculation transparency + audit (no chemistry, no ML). Pure arithmetic
  that documents and **re-derives the downstream math** the app applies on top of PHREEQC output:
  `mgl_to_mM` (uses `ATOMIC_MASSES`), `apply_dilution`, `liquid_solid_ratio`, `mass_released_mg`,
  `recovery_percent`, and `residual`. A `FORMULAS` registry of `Formula` dataclasses (equation,
  LaTeX, inputs, output, units, explanation, provenance `app-calculated` vs `parsed from PHREEQC`,
  plus a dev-mode `detail`) drives the Calculation Verification tab. The **audit engine**
  (`classify` / `audit_residual` / `audit_comparison`) recomputes each `measured − PHREEQC`
  residual from `comparison_measured_vs_phreeqc.csv` and labels it `pass` / `warning` / `fail` /
  `not available` against tolerances (`PASS_TOL=1e-6`, `WARN_TOL=1e-4`). It **explains** PHREEQC's
  saturation index and pH but never recomputes them — PHREEQC stays authoritative. Covered by
  `tests/test_calculations.py`.

- **`app.py`** (repo root) is a thin **Streamlit GUI** over the scripts, reorganized as a
  wide-layout **tabbed dashboard** driven by a run-management **sidebar** (run selector + create-run
  expander; current run name/type/folder/source; a run-type warning; a "go to Run Workflow tab"
  reminder; and a **Developer explanation mode** toggle). The ten tabs are: **Overview** (project +
  selected-run status cards, what's missing, a recommended next step); **Data Entry** (run-type
  specific — lab measured-release form, literature CSV upload + manual rows, or synthetic/demo
  form — plus this run's table, row deletion, and CSV/pipeline export); **Mapping** (lab-like runs
  only: sample_id → PHREEQC record_key upsert/preview/delete/export); **Run Workflow** (one
  primary button that, for a lab run, exports the run CSV + mapping then runs Phase 1 → 07 → 05 →
  08, stopping at the first failure, warning if no mapping; plus an "Advanced individual script
  controls" expander); **Results** (run-type-aware — lab shows the measured-vs-PHREEQC summary,
  comparison/residual figures, pH residual cards, validation + sustainability tables; literature
  shows its own benchmark summary; synthetic shows a testing-only warning); **PHREEQC Outputs**
  (processed-CSV previewer + a filtered PHREEQC-**only** model-output figure viewer — the
  measured-vs-PHREEQC comparison plots live in Results, not here); **Literature Benchmark**
  (literature table + key-columns/comparability summary, shown only for literature runs);
  **Tools** (the experiment-planning scripts 06/07/08 + their output tables, with the **legacy**
  global manual-entry form tucked in a "not recommended" expander); **Calculation Verification**
  (the formula registry, per-row residual audit, mg/L→mM and L/S calculators, and extra
  developer-mode explanations); and **Help / Safety** (workflow, run types, mapping, residuals, and
  limitations). It reuses package functions and adds no chemistry/ML logic. The legacy form appends
  to `data/raw/experimental_icp/experimental_release_manual_entry.csv` (gitignored); the run
  workspace writes into the selected run's own `experiments/<name>/data/`.

### Key conventions

- A PHREEQC solution state is identified by `record_key` = `"<file>|sim<N>|<state>|sol<N>"`; this
  is the join key between PHREEQC results and measured samples.
- Comparisons default to PHREEQC `state == "batch"` (the post-equilibration result that an
  experiment measures).
- Phase 2 is built to be a no-op until data lands: `run_phase1.py` is untouched by Phase 2, and
  step 05 detects a blank template and exits cleanly. Keep this separation when extending.
- `config.CO2_CONDITION_ALLOWED` (`open`/`sealed`/`low_CO2`/`atm_CO2`/`unknown`) is the accepted
  CO₂ vocabulary; the validator errors on anything else, so the plan generator, the Streamlit
  dropdown (which derives its options from this list), and any sample entry must use these exact
  labels. There is no separate "none/atmospheric/elevated" set — those older labels were removed.
- **Fe is often unpredicted.** The CEMDATA18 runs may omit `mol_Fe`, so `phreeqc_Fe_mM` and
  `residual_Fe` can be entirely NaN. Step 05 prints an explicit WARNING when Fe is *measured* but
  PHREEQC has no Fe prediction — this is "unavailable", not "PHREEQC predicts zero Fe".
- The `sample_id` format `CFA-NaOH{M}M-LS{ratio}-{min}min-{CO2}-R{rep}` (built by
  `plan_generator.make_sample_id`) is the human-facing link from run sheet → filled release CSV →
  `sample_phreeqc_map.csv` → comparison. It's the dedup key in the plan (replicates kept distinct),
  so keep it stable.
