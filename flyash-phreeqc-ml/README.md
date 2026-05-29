# flyash-phreeqc-ml

Machine-learning-assisted geochemical modeling of **coal fly ash (CFA) + metakaolin (MK)**
alkali-activated systems, combining **PHREEQC** speciation/equilibrium simulations with
**experimental ICP** measurements.

## Ultimate goal

Use PHREEQC outputs **plus** experimental ICP data to predict measured fly-ash outcomes —
especially the release of **Ca, Si, Al, Fe, REE/Sc**, the resulting **pH**, **carbonate
formation**, and **selectivity**. The ML layer should eventually learn *where PHREEQC
disagrees with experiment* (a correction/residual model), not blindly replace the chemistry.

The project is built in phases. **Only Phase 1 is implemented so far.**

| Phase | Scope | Status |
|-------|-------|--------|
| **1. Parse + analyze** | Robust parsers for `.pqi`, `.pqo`, `SELECTED_OUTPUT`, ICP Excel/CSV → clean processed CSVs → `master_dataset.csv` → basic plots (Ca, Si, Al, Fe, pH, saturation indices) | ✅ implemented |
| 2. PHREEQC vs experiment | Align PHREEQC predictions with measured ICP; compute residuals/errors | ⬜ planned |
| 3. ML correction/surrogate | Simple models predicting measured outcomes from PHREEQC + input variables | ⬜ planned |

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
│   └── viz/
│       └── plots.py             # basic exploratory plots
├── scripts/                     # thin command-line entry points (run these)
│   ├── 01_parse_phreeqc.py
│   ├── 02_parse_icp.py
│   ├── 03_build_master_dataset.py
│   ├── 04_make_plots.py
│   └── run_phase1.py            # runs all of the above in order
├── data/
│   ├── raw/                     # original inputs (committed, read-only)
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
