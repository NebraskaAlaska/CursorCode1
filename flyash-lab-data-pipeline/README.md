# Fly Ash Lab Data Pipeline & Auto-Report Generator

A Streamlit application for **Class C fly ash reuse research**. Upload lab data
(CSV/Excel), validate and clean it, compute derived metrics (binder ratios, CO₂/cost
savings, strength statistics), generate plots, rank mix designs for reuse applications,
and export an automatic HTML research report.

Red mud is treated as a **small optional additive / comparison variable**, never a
primary input — supply is assumed to be limited, so lower red-mud demand scores higher.

> All CO₂/cost factors and scoring weights are **editable assumptions**, not fixed
> scientific facts. Tune them in the app sidebar.

---

## Features

- **8 guided tabs:** Upload → Data Quality Check → Mix Design Summary → Strength
  Results → Leachate Results → CO₂/Cost Estimate → Reuse Ranking → Auto Report.
- **Flexible input:** CSV or Excel; forgiving column-name matching; missing columns
  are added automatically. Downloadable blank CSV/Excel template.
- **Clear ID model:** `mix_id` (formulation) · `specimen_id` (individual cube/cylinder)
  · `test_id` (optional unique test) · `sample_id` (kept for convenience).
- **Strength back-calculation:** if `compressive_strength_MPa` is missing but
  `peak_load_kN` and an area (explicit `loaded_area_mm2`, or from shape + dimensions)
  are present, strength is computed (`MPa = kN·1000 / mm²`) and flagged as `calculated`.
- **Data status workflow:** `pending` · `tested` · `failed` · `needs_retest`
  (inferred when blank; explicit values preserved).
- **Validation:** duplicate/missing IDs, negative masses, zero binder, implausible
  water/binder ratio, pH out of 0–14, missing curing age, missing strength
  (a *warning*, since specimens may not have reached curing age), high coefficient
  of variation, and conductivity-unit checks.
- **Plots (Plotly):** strength vs age, strength vs fly-ash %, pH vs age, conductivity
  vs fly-ash %, CO₂ vs strength, flow vs w/b, reuse score by mix.
- **Reuse ranking:** per-application weighting presets (cement replacement, flowable
  fill, blocks/pavers, road base, stabilized disposal monolith), tunable via sliders.
- **Exports:** cleaned CSV, processed CSV (with derived metrics), and a self-contained
  HTML report (saved to `reports/`).

---

## Installation

Requires **Python 3.10+** (developed on 3.12).

```bash
cd flyash-lab-data-pipeline
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
```

## Running the app

```bash
streamlit run app.py
```

Streamlit opens the app in your browser (default <http://localhost:8501>).

## Uploading data & generating a report

1. **Upload Data** tab — download the CSV or Excel template, fill it in, and upload it
   (or upload your own file with matching column names).
2. **Data Quality Check** — review errors (fix before trusting rankings) and warnings.
3. Browse **Mix Design Summary**, **Strength Results**, **Leachate Results**, and
   **CO₂/Cost Estimate** to inspect derived metrics and plots.
4. Adjust **CO₂/cost factors** and **scoring weights** in the sidebar — changes apply
   live.
5. **Reuse Ranking** — see each mix scored 0–100 per application.
6. **Auto Report** — export cleaned/processed CSVs and click **Build report** to
   generate an HTML report (downloadable and saved to `reports/`).

---

## Expected data columns

Identity: `sample_id`, `mix_id`, `specimen_id`, `test_id`
Dates: `date_cast`, `date_tested`
Mix masses (g): `fly_ash_mass_g`, `cement_mass_g`, `water_mass_g`, `red_mud_mass_g`,
`sand_mass_g`
Geometry/load (optional): `specimen_shape`, `length_mm`, `width_mm`, `diameter_mm`,
`loaded_area_mm2`, `peak_load_kN`
Measurements: `curing_age_days`, `flow_mm`, `setting_time_min`,
`compressive_strength_MPa`, `leachate_pH`, `leachate_conductivity_uS_cm`
Other: `mix_type`, `additive_type`, `data_status`, `visual_notes`, `photo_path`

Missing columns are tolerated — the pipeline adds them as empty.

## Derived metrics

`total_binder_mass_g`, `water_binder_ratio`, `fly_ash_replacement_percent`,
`red_mud_percent`, `estimated_cement_saved_g`, `estimated_co2_saving_kg`,
`estimated_cost_saving`, per-(mix, age) `mean_MPa` / `std_MPa` / `cv_percent`,
`strength_source`, and inferred `data_status`.

---

## Project structure

```
flyash-lab-data-pipeline/
├── app.py                  # Streamlit app (8 tabs)
├── requirements.txt
├── README.md
├── data/
│   ├── raw/                # your input files (optional)
│   ├── processed/          # exported processed data (gitignored)
│   └── templates/          # generated blank templates
├── reports/                # generated HTML reports (gitignored)
├── src/
│   ├── config.py           # schema, editable assumptions, thresholds, scoring presets
│   ├── data_loader.py      # CSV/Excel load, normalise, templates, exports
│   ├── calculations.py     # derived metrics, strength back-calc, statistics
│   ├── validation.py       # data-quality rules
│   ├── scoring.py          # leaching risk + per-application reuse ranking
│   ├── plotting.py         # Plotly figure builders
│   └── report_generator.py # HTML report assembly (Jinja2)
├── templates/
│   └── report_template.html
└── tests/
    ├── test_calculations.py
    ├── test_validation.py
    └── test_scoring.py
```

## Running tests

```bash
pytest
```

## Roadmap / not yet implemented

- PDF export of the report (HTML is the current MVP output).
- Manual in-app sample data entry (upload is the priority path).
