# flyash-phreeqc-ml

Machine-learning-assisted geochemical modeling of **coal fly ash (CFA) + metakaolin (MK)**
alkali-activated systems, combining **PHREEQC** speciation/equilibrium simulations with
**experimental ICP** measurements.

## Ultimate goal

Use PHREEQC outputs **plus** experimental ICP data to predict measured fly-ash outcomes —
especially the release of **Ca, Si, Al, Fe, REE/Sc**, the resulting **pH**, **carbonate
formation**, and **selectivity**. The ML layer should eventually learn *where PHREEQC
disagrees with experiment* (a correction/residual model), not blindly replace the chemistry.

The project is built in phases.

| Phase | Scope | Status |
|-------|-------|--------|
| **1. Parse + analyze** | Robust parsers for `.pqi`, `.pqo`, `SELECTED_OUTPUT`, ICP Excel/CSV → clean processed CSVs → `master_dataset.csv` → basic plots (Ca, Si, Al, Fe, pH, saturation indices) | ✅ implemented |
| **2. PHREEQC vs experiment** | Measured-release template + parser, residuals (measured − PHREEQC), measured-vs-PHREEQC plots | 🟡 scaffolding ready (awaiting measured data) |
| 3. ML correction/surrogate | Simple models predicting measured outcomes from PHREEQC + input variables | ⬜ planned (no ML yet) |

> **Phase 2 status:** the ingestion + comparison machinery is in place and tested, but
> **no real ML is trained yet**. The comparison runs as soon as measured experimental
> data is dropped in (see *Entering Monday's experimental data* below).

## Project layout

```
flyash-phreeqc-ml/
├── flyash_phreeqc_ml/          # the importable package (inspect each module in Cursor)
│   ├── config.py               # all paths + domain constants in one place
│   ├── parsers/
│   │   ├── pqi_parser.py        # PHREEQC INPUT  (.pqi)  -> solutions + equilibrium phases
│   │   ├── pqo_parser.py        # PHREEQC OUTPUT (.pqo)  -> speciation / SI / phase assemblage
│   │   ├── selected_output_parser.py  # SELECTED_OUTPUT tables (.out/.sel/.tsv/.csv)
│   │   └── icp_parser.py        # experimental ICP Excel/CSV
│   ├── datasets/
│   │   └── build_master.py      # join everything into master_dataset.csv
│   ├── compare/                 # Phase 2: measured vs PHREEQC
│   │   └── residuals.py         # residual_<X> = measured − PHREEQC
│   └── viz/
│       ├── plots.py             # Phase 1 exploratory plots
│       └── compare_plots.py     # Phase 2 measured-vs-PHREEQC plots
├── scripts/                     # thin command-line entry points (run these)
│   ├── 01_parse_phreeqc.py
│   ├── 02_parse_icp.py
│   ├── 03_build_master_dataset.py
│   ├── 04_make_plots.py
│   ├── 05_compare_experimental.py   # Phase 2 (no-op until measured data exists)
│   └── run_phase1.py            # runs steps 01–04 in order
├── tests/                       # pytest suite (Phase 2 ingestion + residuals)
├── data/
│   ├── raw/                     # original inputs (committed, read-only)
│   │   └── experimental_icp/    # measured-release template + Monday's lab CSVs
│   └── processed/               # generated CSVs  (created by the scripts)
└── reports/figures/             # generated plots  (created by the scripts)
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # pandas / numpy / matplotlib / openpyxl
```

The scripts add the project root to `sys.path` themselves (see `scripts/_path_setup.py`),
so they run straight from Cursor without installing the package. If you prefer importing
`flyash_phreeqc_ml` from anywhere (notebooks, other tools), you can also
`pip install -e .` — it's optional.

## Run Phase 1

```bash
# everything at once
python scripts/run_phase1.py

# or step by step
python scripts/01_parse_phreeqc.py        # -> data/processed/phreeqc_*.csv
python scripts/02_parse_icp.py            # -> data/processed/icp_*.csv
python scripts/03_build_master_dataset.py # -> data/processed/master_dataset.csv
python scripts/04_make_plots.py           # -> reports/figures/*.png
```

## Outputs of Phase 1

- `data/processed/phreeqc_input_solutions.csv` — input solution compositions (from `.pqi`)
- `data/processed/phreeqc_results.csv` — one row per simulated solution state, with pH, pe,
  ionic strength, alkalinity, element molalities, key saturation indices, and phase deltas
- `data/processed/phreeqc_saturation_indices.csv` — long/tidy table of every SI per solution
- `data/processed/phreeqc_phase_assemblage.csv` — long/tidy phase-assemblage deltas
- `data/processed/icp_*.csv` — best-effort extraction of the ICP workbook (one CSV per sheet)
- `data/processed/master_dataset.csv` — the joined modeling table (PHREEQC side for now)
- `reports/figures/*.png` — exploratory plots

## Entering Monday's experimental data (Phase 2)

The lab/ICP results are entered against a fixed template so the parser, comparison,
and tests all agree on the schema.

**1. Copy the template.** It lives at:

```
data/raw/experimental_icp/experimental_release_template.csv
```

Copy it to a new, dated file in the **same folder** — e.g.
`data/raw/experimental_icp/2026-06-01_release.csv`. Keep the template itself blank
(the pipeline always skips it).

**2. Fill one row per measured sample.** Columns (order doesn't matter; extra columns
are allowed and preserved):

| Column | Meaning / units |
|--------|-----------------|
| `sample_id` | unique id for the sample (used to link to PHREEQC) |
| `experiment_date` | `YYYY-MM-DD` |
| `fly_ash_type` | e.g. `CFA`, `CFA+MK` |
| `NaOH_M` | activator NaOH molarity |
| `time_min` | reaction/leaching time (minutes) |
| `temperature_C` | temperature (°C) |
| `liquid_solid_ratio` | L/S ratio |
| `CO2_condition` | e.g. `none` / `atmospheric` / `elevated` |
| `initial_pH`, `final_pH` | measured pH before/after |
| `conductivity_mS_cm` | conductivity (mS/cm) |
| `Ca_mM`, `Si_mM`, `Al_mM`, `Fe_mM`, `Na_mM`, `K_mM` | measured concentrations (**mM**) |
| `Sc_ppb`, `total_REE_ppb` | trace concentrations (**ppb**) |
| `filtration_notes`, `precipitate_observed`, `notes` | free text |

Leave any unknown numeric cell **blank** (it becomes `NaN`); don't write `n/a`.

**3. (Optional but needed for residuals) link each sample to a PHREEQC run.**
Residuals are `measured − PHREEQC`, so each `sample_id` must point at the PHREEQC
`record_key` representing the same chemistry. Create:

```
data/raw/experimental_icp/sample_phreeqc_map.csv
```

with two columns:

```
sample_id,phreeqc_record_key
S001,L-S_5_revised.pqo|sim1|batch|sol1
```

(`record_key` values come from `data/processed/phreeqc_results.csv`.) Without this
map the comparison still runs, but the PHREEQC columns and residuals stay `NaN`.

**4. Run the comparison:**

```bash
python scripts/01_parse_phreeqc.py        # ensure phreeqc_results.csv exists
python scripts/05_compare_experimental.py # measured vs PHREEQC + residuals + plots
```

Outputs (only when measured data is present):

- `data/processed/experimental_release.csv` — parsed, type-checked measured data
- `data/processed/comparison_measured_vs_phreeqc.csv` — joined table with
  `residual_Ca`, `residual_Si`, `residual_Al`, `residual_Fe`, `residual_pH`
- `reports/figures/measured_vs_phreeqc.png` — scatter vs 1:1 line (if samples are linked)
- `reports/figures/residuals_by_sample.png`

If only the blank template is present, step 5 prints a notice and exits without
writing anything — so it's always safe to run.

> **Units:** PHREEQC reports element totals as molality (mol/kgw); the comparison
> multiplies by 1000 to get mM (`config.PHREEQC_MOLALITY_TO_MM`) so it matches the
> measured `*_mM` columns. For dilute solutions mol/kgw ≈ mol/L.

## Tests

```bash
pip install -r requirements-dev.txt   # pytest
pytest
```

The suite covers Phase-2 ingestion (`tests/test_experimental_ingestion.py`) — template
schema, dtype coercion, missing/extra columns, measured-data detection, directory
loading — and the residual math (`tests/test_comparison.py`).

## Notes on the data

- PHREEQC inputs use the **CEMDATA18** cement-chemistry database; solutions are high-pH
  (~13) Na–Si–Al–Ca systems equilibrated with `CO2(g)` (carbonation), tracking calcite (`Cal`),
  aragonite (`Arg`) and portlandite.
- The `.pqo` files are the verbose PHREEQC text output. The parser reads the
  *Solution composition*, *Description of solution*, *Phase assemblage* and *Saturation
  indices* blocks. The cleaner machine-readable alternative is `SELECTED_OUTPUT` (the revised
  input requests it) — `selected_output_parser.py` reads those tables directly when present.
- The ICP workbook (`CFA + MK design mix_UMass.xlsx`) is a **mix-design calculator**, not a
  tidy data table (subscripts are split across cells, many sub-tables per sheet). Phase 1 does
  a *best-effort* dump to CSV; turning it into a clean measured-results table is part of Phase 2
  and may need a small hand-written mapping once the relevant cells are confirmed.
