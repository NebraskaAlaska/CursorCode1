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
  + the app's **Calculation Verification** view (in the Audit / Help tab) document every downstream formula (residuals, ICP
  mg/L→mM, dilution, L/S ratio, mass released, recovery) and **re-derive** the stored residuals to
  confirm they match (`pass`/`warning`/`fail`/`not available`). PHREEQC's SI and pH are explained,
  not recomputed.
- **Mapping assistant + scenario explorer** (guided mapping, no ML). `flyash_phreeqc_ml/scenarios.py`
  builds a readable **PHREEQC scenario manifest** from `phreeqc_results.csv` (molality→mM, metadata
  inferred from the source filename where safe, else `unknown`) and scores each scenario against a
  measured sample with **transparent rule-based** weights (no learning) so the Match PHREEQC tab can
  *suggest* the best PHREEQC rows, flag samples needing a new simulation, and warn on collisions.
  Plus **lab CSV upload** for lab runs, **mapping-quality** checks, and the app no longer generates
  experiment plans — its job is now ingest → verify → map → run → interpret.
- **Guided-workflow UI** (UI-only reorg, no chemistry/ML). The Streamlit app was condensed from ten
  scattered tabs into **five workflow tabs** — **Start**, **Data**, **Match PHREEQC**, **Run +
  Results**, **Audit / Help** — matching the ingest → verify → map → run → interpret order. No
  functionality was removed: it was relocated (e.g. Run Workflow + Results merged; PHREEQC Outputs +
  Calculation Verification + Help folded into Audit / Help; the old Tools/Literature tabs absorbed
  into Data / Run + Results). Start adds a data-quality line, a richer recommended-next-action, and a
  workflow checklist.
- **Flexible + dissolution-workbook import** (ingest, no chemistry/ML). The Data tab's lab uploader
  is now two modes (`_lab_data_import`): a **generic** `.csv`/`.xlsx`/`.xls` importer
  (`import_mapping.py` — sheet pick, fuzzy column mapping, mg/L·ppm·ppb→mM, leachant/provenance,
  pre-save validation, confirm-gated replace/append) and a special-case **Class C fly ash
  dissolution-workbook** parser (`dissolution_workbook.py` — horizontal ICP OES element blocks with
  mmol/l preferred, pH read from the pH column, HCl kept acid-tagged), validated against the real
  file. Both keep unknown columns and write through `run_manager.save_lab_dataframe`.
- **Replicate-aware mapping + presentation status** (no chemistry/ML). `replicates.py` treats PHREEQC
  `sol1/sol2/sol3` as replicate/batch outputs of one condition, not time points: `condition_key`
  grouping, replicate ids, mean±std summaries, **condition-level mapping** (replicates inherit one
  link, expanded to the per-sample map; storage in `run_manager`), an optional replicate→solution
  path, replicate-aware collision rules, and condition mean / individual comparison modes. Layered on
  top, a **mapping-status** classifier (`exact` / `scenario-level only` / `unsafe` / `needs new
  PHREEQC simulation`) drives the Start **Presentation summary**, the Run + Results "workflow check,
  not final validation" warning, and the "conditions needing new PHREEQC simulations" table — framing
  the comparison as a **preliminary validation workflow**, not an overclaimed model.

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
  run type. **Lab CSV upload:** `save_lab_dataframe(run_name, df, mode="replace"|"append")` writes
  an uploaded measured-release CSV through `lab_release_path` (so the `RunTypeError` guardrail still
  blocks literature/synthetic data); `missing_lab_required_columns` validates against
  `LAB_REQUIRED_COLUMNS` (the 10 metadata+pH columns every measured row needs). **Sample→PHREEQC
  mapping** (lab-like runs only): `add_mapping` upserts one `sample_id → phreeqc_record_key` link
  (columns `MAPPING_COLUMNS`, matching what `scripts/05` + `compare/residuals.py` expect),
  `read_mapping` / `delete_mapping_rows` / `has_mapping` manage it in
  `experiments/<run>/data/sample_phreeqc_map.csv`, `export_mapping_to_pipeline` copies it to
  `data/raw/experimental_icp/sample_phreeqc_map.csv` for step 05, and `summarize_mapping` (pure)
  reports samples / unique PHREEQC rows / samples-per-row + collisions for the mapping-quality
  warnings.

- **`calculations.py`** — calculation transparency + audit (no chemistry, no ML). Pure arithmetic
  that documents and **re-derives the downstream math** the app applies on top of PHREEQC output:
  `mgl_to_mM` (uses `ATOMIC_MASSES`), `apply_dilution`, `liquid_solid_ratio`, `mass_released_mg`,
  `recovery_percent`, and `residual`. A `FORMULAS` registry of `Formula` dataclasses (equation,
  LaTeX, inputs, output, units, explanation, provenance `app-calculated` vs `parsed from PHREEQC`,
  plus a dev-mode `detail`) drives the Calculation Verification block (in the **Audit / Help** tab).
  The **audit engine**
  (`classify` / `audit_residual` / `audit_comparison`) recomputes each `measured − PHREEQC`
  residual from `comparison_measured_vs_phreeqc.csv` and labels it `pass` / `warning` / `fail` /
  `not available` against tolerances (`PASS_TOL=1e-6`, `WARN_TOL=1e-4`). It **explains** PHREEQC's
  saturation index and pH but never recomputes them — PHREEQC stays authoritative. Covered by
  `tests/test_calculations.py`.

- **`scenarios.py`** — PHREEQC scenario manifest + rule-based mapping assistant (no chemistry, no
  ML). `build_scenario_manifest` / `write_scenario_manifest` turn `phreeqc_results.csv` into
  `data/processed/phreeqc_scenario_manifest.csv` (`MANIFEST_COLUMNS`): molality→mM via
  `config.PHREEQC_MOLALITY_TO_MM` (missing `mol_Fe` → NaN, not zero), a readable `scenario_label`,
  and metadata `infer`-red from the source filename only where safe (`L-S_5`→L/S 5; `atmCO2`→
  `atm_CO2`; `lowCO2`→`low_CO2`; `noCO2`→`sealed`; else `unknown`, with `metadata_quality`
  good/partial/unknown). `score_scenario` applies **hand-written** rules (+3 batch / −4 initial /
  +3 L/S match / +2 compatible CO₂ / +1 temp match-or-unknown / −2 major conflict); `confidence_for`
  bands the raw score high/medium/low (`HIGH_SCORE=7`, `MEDIUM_SCORE=4`), but `_metadata_alignment`
  then **caps** confidence to *medium* whenever the experiment specifies metadata the PHREEQC
  manifest lacks — `time_min`, an OA/PF/GS `condition_code` (`sample_condition_code` reads
  `extra__condition_code`/`sample_id`/`notes`), or `NaOH_M` — so **high requires both sides to
  align**, not just L/S + CO₂ + batch. The cap returns `base_confidence`, `phreeqc_missing`, and
  human `metadata_notes` (e.g. "Experimental time is known, but the selected PHREEQC scenario does
  not specify time.") that the Match PHREEQC tab shows per suggestion. `suggest_mappings` returns the
  top-N scenarios, `samples_needing_simulation` flags unmapped/low-confidence/colliding samples, and
  `describe_solutions` / `load_solution_descriptions` summarise each `.pqi` `SOLUTION n` label so the
  app can explain that **sol1/sol2/sol3 are PHREEQC solution numbers, not time points or replicates**
  unless the input defines them so. Covered by `tests/test_scenarios.py`.

- **`import_mapping.py`** — flexible experimental-file import (ingest helper, no chemistry, no ML).
  Pure functions the Data-tab lab uploader wires to widgets: `file_kind` / `list_excel_sheets` /
  `read_tabular` read `.csv`/`.xlsx`/`.xls` (one Excel sheet at a time); `suggest_column_mapping`
  maps uploaded headers onto the release schema (`MAPPING_TARGETS` = `EXPERIMENTAL_RELEASE_COLUMNS`
  + optional `leachant`/`acid_M`) via **hand-written** `COLUMN_SYNONYMS` (two passes — exact name
  wins over alias, one source column used once); `convert_concentration` / `convert_series_to_mM`
  convert mg/L·ppm·ppb → mM reusing `calculations.ATOMIC_MASSES` (`mM = mg/L / atomic_mass`,
  ppb = µg/L; Sc/REE stay ppb). `build_schema_frame` produces a schema-aligned frame: chemistry
  unit-converted, `leachant`/`acid_M` filled (acid rows — `is_acid_leachant` — get `NaOH_M` blanked
  + an `ACID_IMPORT_NOTE`, never forced into `NaOH_M`), provenance (`PROVENANCE_COLUMNS`:
  file/sheet/row/timestamp/warning/units) and unknown columns (`extra__` prefix) appended so
  nothing is dropped silently. `summarize_import` is the pre-save report (missing required, pH
  outside 0–14, blank/duplicate sample_ids, no-measured-value rows, converted columns, row
  classification pH-only/chemistry-present/incomplete). The extra columns ride through
  `run_manager.save_lab_dataframe` (which keeps non-canonical columns after the schema), so the
  existing pipeline is unaffected. Covered by `tests/test_import_mapping.py`.

- **`dissolution_workbook.py`** — special-case parser for the Class C fly ash **dissolution
  workbook** (ingest helper, no chemistry, no ML), an *additional* Data-tab import mode beside the
  generic `import_mapping` one (never a replacement). The workbook is non-rectangular. The **ICP OES**
  sheet lays unit groups out **horizontally**: a single top row holds `mg/L` and `mmol/l`, each
  anchoring a group of condition columns (`NaOH-OA/PF/GS`); each element block (Calcium/Silicon/
  Aluminum) then has one shared header row spanning *both* unit groups, with reaction-time rows. The
  **pH** sheet has a `Sample | Time (min) | pH` label list (incl. HCl rows) plus a NaOH pH matrix.
  Parsing is **marker-based**, not fixed cell coordinates (`ELEMENT_TO_COLUMN`, `_match_unit`,
  `CONDITION_RE`, `LABEL_RE`): `_unit_column_map` assigns each column to its unit group from the
  global unit row; `parse_icp_sheet` reads each block long, picks each cell's unit by column, and
  **prefers `mmol/l`** (converting `mg/L`→mM via `import_mapping.convert_concentration` only as a
  fallback); `parse_ph_sheet` reads pH from the **pH header column** (not the Time column) for
  explicit labels, plus a NaOH pH-matrix pass for matrix-only times (e.g. 20 min). `"-"`/blank cells
  are treated as **missing, never 0**. `normalize_dissolution_workbook` joins chemistry onto NaOH pH
  rows by `(condition_code, time_min)`, keeps **HCl rows pH-only + acid-tagged** (`NaOH_M` blank,
  `acid_M` set, `ACID_IMPORT_NOTE`), fills operator-supplied metadata from a `defaults` dict
  (`DEFAULT_FILL_FIELDS`; `fly_ash_type` defaults `Class C fly ash`), and honours an `include_hcl`
  scope. It returns `(schema_df, report)` where `report` has parse counts
  (NaOH/HCl/with-pH/with-chem/missing-metadata), warnings (Fe/Na/K/Sc/REE absent; OA/PF/GS meanings
  unknown; HCl ≠ NaOH PHREEQC), and `icp_debug` (per-element time×condition pivots for the app's
  debug view, via `icp_debug_pivots`). OA/PF/GS are preserved in `sample_id`, `notes`, and an
  `extra__condition_code` column. Reuses `import_mapping`'s leachant/provenance columns so saved rows
  match a generic import. Validated against the real workbook layout and a synthetic fixture in
  `tests/test_dissolution_workbook.py`; the marker constants (sheet/element/unit/condition labels)
  are the tuning points if another workbook differs.

- **`replicates.py`** — replicate-aware mapping layer (no chemistry, no ML). In this project PHREEQC
  `sol1/sol2/sol3` are **replicate batches of one experimental condition**, not time points, so a
  measured row is *(condition, replicate batch)* rather than a sample mapped straight to a solution
  number. `condition_key` collapses leachant + molarity (`acid_M` for acids) + OA/PF/GS code + time +
  L/S + CO2 + temp into one stable grouping key (e.g. `NaOH0.5M_OA_10min_LS5_open`); `replicate_id` /
  `parse_replicate_id` read `R1/rep2/batch3` from the sample_id (`infer_replicate_ids` fills blanks by
  order **with a warning**); `replicate_summary` reports count + mean ± std (ddof=1, NaN for a single
  replicate) of pH/Ca/Si/Al per condition. `expand_condition_mapping` turns one `condition_key →
  record_key` link into the per-sample map the pipeline reads (all replicates inherit it);
  `replicate_record_key` / `expand_replicate_solution_mapping` are the optional advanced path where
  each replicate points at its own `solN`. `condition_mean_comparison` (mean ± std vs PHREEQC,
  `residual = mean − PHREEQC`, n<2 flag) and `individual_replicate_comparison` are the two results
  modes. `collision_report` is replicate-aware: same-condition replicates sharing a PHREEQC scenario
  is **expected** (not flagged); it warns only on **different** condition_keys sharing a scenario,
  acid→NaOH mappings, and (via `scenarios._metadata_alignment`) time/condition metadata PHREEQC
  can't confirm. Storage lives in `run_manager` (`condition_phreeqc_map.csv`,
  `replicate_solution_map.csv`; `add_condition_mapping` / `apply_condition_mapping` expand to the
  run's `sample_phreeqc_map.csv`). Covered by `tests/test_replicates.py`. Surfaced in the Match
  PHREEQC tab (replicate summary + condition-level mapping + advanced replicate→solution expander +
  replicate-aware collision warnings) and Run + Results (comparison-mode radio + condition mean±std
  error-bar plot + individual-replicate scatter). For **presentation honesty**, `mapping_status`
  classifies a sample→scenario link as `exact` / `scenario-level only` / `unsafe` / `needs new
  PHREEQC simulation` (`MAPPING_STATUS_DEFINITIONS`), `overall_mapping_status` aggregates it (with
  `all_exact`), and `conditions_needing_simulation` is the presentation table
  (`CONDITIONS_NEEDED_COLUMNS`). The Start tab's **Presentation summary** (`_render_presentation_summary`)
  surfaces dataset/validation/mapping counts, overall mapping + comparison status, a recommended next
  *scientific* step, and the standing caveat that the comparison is **preliminary / a workflow check
  unless mappings are exact**; the same valid-now / not-yet wording and status definitions appear in
  Audit / Help, and Run + Results shows the "residual plots are a workflow check, not final
  validation" warning whenever any mapping is not exact.

- **`app.py`** (repo root) is a thin **Streamlit GUI** over the scripts, organized as a
  wide-layout **guided five-tab workflow** driven by a run-management **sidebar** (run selector +
  create-run expander; current run name/type/folder/source; a run-type warning; a "go to Run +
  Results tab" reminder; and a **Developer explanation mode** toggle). The five tabs follow the
  ingest → verify → map → run → interpret order:
  1. **Start** (`_render_overview`) — project status cards + selected-run summary (run type, data
     rows, mapped samples, unique PHREEQC rows used), a one-line **data-quality status**, what's
     missing, a **recommended next action** (no-data / no-mapping / coarse-mapping / workflow-not-run
     / ICP-missing / mock-data cases), and a **workflow checklist** (Data uploaded → Data checked →
     Mapping complete → Workflow run → Results available).
  2. **Data** (`_render_data_entry_tab`) — run-type specific: lab measured-release form **plus a
     two-mode "Upload experimental data file"** importer (`_lab_data_import` mode radio):
     **Generic table** (`_generic_table_import`, `.csv`/`.xlsx`/`.xls` via `import_mapping`: raw
     preview → Excel sheet pick → column mapping → unit conversion → leachant/provenance → pre-save
     validation → confirm-gated replace/append) **or Class C fly ash dissolution workbook**
     (`_dissolution_import` via `dissolution_workbook`: shared metadata defaults → NaOH-only/NaOH+HCl
     scope → marker-based parse → normalised preview with parse counts + warnings → confirm-gated
     save) — literature CSV upload + manual rows, or synthetic/demo form — plus this run's table,
     row deletion, CSV/pipeline export, a **basic validation summary** (error/warning counts via the
     07 validator, lab runs), and the **legacy global manual-entry form** under a "not recommended"
     expander.
  3. **Match PHREEQC** (`_render_mapping_tab`, lab-like runs only) — a **PHREEQC Scenario Explorer**
     (filterable manifest table), a **Mapping Assistant** (pick a sample → top-3 rule-scored
     suggestions with confidence + "Use this mapping" buttons, a no-good-match warning), a
     mapping-quality summary with collision/coarse warnings, a "samples needing new PHREEQC
     simulations" table, existing-mapping upsert/preview/delete/export, and the original dropdown
     kept under an **"Advanced manual mapping"** expander.
  4. **Run + Results** (`_render_run_and_results_tab`) — combines workflow execution and results: one
     primary button that, for a lab run, exports the run CSV + mapping then runs Phase 1 → 07 → 05 →
     08, stopping at the first failure, warning if no mapping (plus an "Advanced individual script
     controls" expander); then the run-type-aware results — lab shows the measured-vs-PHREEQC
     summary, comparison/residual figures, an interpretation note on coarse mapping, pH residual
     cards, validation + sustainability tables; literature shows its own benchmark summary; synthetic
     shows a testing-only warning.
  5. **Audit / Help** (`_render_audit_help_tab`) — the Calculation Verification block (formula
     registry, per-row residual audit, mg/L→mM and L/S calculators, developer-mode explanations),
     the **PHREEQC raw outputs** (processed-CSV previewer + PHREEQC-**only** model-output figure
     viewer) under expanders, and the **Help / Safety** reference (workflow, run types, mapping,
     residuals, limitations).

  It reuses package functions and adds no chemistry/ML logic. Tables are height-limited so they don't
  stretch the page; advanced content (raw PHREEQC tables, individual scripts, formula-audit details,
  legacy/global data entry, advanced manual mapping) lives in expanders. The legacy form appends to
  `data/raw/experimental_icp/experimental_release_manual_entry.csv` (gitignored); the run workspace
  writes into the selected run's own `experiments/<name>/data/`. **Experiment-plan generation (06)
  is not surfaced in the UI** — the app no longer creates plans.

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
  PHREEQC has no Fe prediction — this is "unavailable", not "PHREEQC predicts zero Fe". The scenario
  manifest follows the same rule: a missing molality column → NaN prediction, never zero.
- **The app ingests, it does not plan.** The Streamlit app's purpose is ingest → verify → map →
  run → interpret. Experiment-plan generation (`scripts/06_generate_experiment_plan.py`) was removed
  from the UI; the script still exists and is runnable from the CLI, but no button surfaces it.
- **Scenario metadata is inferred conservatively.** `scenarios.infer_metadata_from_filename` only
  reads tokens it is sure of (`L-S_<n>`, `atmCO2`/`lowCO2`/`noCO2`); everything else (notably
  `NaOH_M`, never in these filenames) stays `unknown` rather than being guessed. The mapping
  assistant's scores are **hand-written rules, not learned weights** — keep them transparent.
- The `sample_id` format `CFA-NaOH{M}M-LS{ratio}-{min}min-{CO2}-R{rep}` (built by
  `plan_generator.make_sample_id`) is the human-facing link from run sheet → filled release CSV →
  `sample_phreeqc_map.csv` → comparison. It's the dedup key in the plan (replicates kept distinct),
  so keep it stable.
