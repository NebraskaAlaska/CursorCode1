# Experiment runs (save files)

This folder holds **experiment runs** — lightweight "save files" so one app can
keep several different experiments side by side without mixing their data. It is
an app-level save/open layer, **not** a project-management system, and it does
**not** replace the `data/raw/experimental_icp/` Phase-2 pipeline workflow.

## Layout

Each run is a folder named after a filesystem-safe version of your run name:

```
experiments/<safe_run_name>/
    run_config.yaml      # run metadata (type, description, created_at, …)
    data/                # this run's data CSV(s)
    outputs/             # this run's generated outputs (figures, tables)
```

The data file inside `data/` depends on the run type:

| run_type               | data file                      | schema                                   |
|------------------------|--------------------------------|------------------------------------------|
| `lab_experiment`       | `data/experimental_release.csv`| canonical measured-release columns       |
| `plastic_composite`    | `data/experimental_release.csv`| same release columns (side project)      |
| `literature_benchmark` | `data/literature_benchmark.csv`| literature/reported columns (separate)   |
| `synthetic_demo`       | `data/demo_data.csv`           | release columns + `source_type` tag      |

## Run types

- **`lab_experiment`** — *real measured lab data* from our experiments. Enter
  pH-only data now (leave Ca/Si/Al/Fe/Na/K/Sc/REE blank) and add ICP numbers to
  the same rows later. This is the only kind of data that represents our actual
  measurements.
- **`literature_benchmark`** — values *reported by other papers*, for comparison
  only. **Kept in a separate file** so they can never be confused with our own
  measurements. Do not treat literature numbers as our experiment.
- **`synthetic_demo`** — fake/demo data for testing the code. Every row is tagged
  `source_type = synthetic_demo`. Not for scientific conclusions.
- **`plastic_composite`** — a plastic / fly-ash composite side project. Uses the
  release schema but stays in its own run folder.

## How to use it (Streamlit app)

In `streamlit run app.py`, the sidebar **"Experiment runs"** section lets you:

1. **Create a new run** — pick a name and a run type, add a short description.
2. **Select an existing run** — the rest of the app then reads/writes that run.
3. See the selected run, its folder path, and a run-type warning.

Then, depending on the run type, the main panel lets you enter rows, upload a CSV
(literature), preview the table, and export the run's CSV.

### Entering pH-only data, then ICP later

For a `lab_experiment` run, submit a row with just `sample_id` and the pH fields
filled — every chemistry column may be blank. Later, when ICP results come back,
add new rows (or re-enter) with the `*_mM` / `*_ppb` columns filled. The schema is
the standard release schema, so nothing special is needed.

### Why literature must stay separate

Literature values are other people's reported results under other people's
conditions. Mixing them into our `experimental_release.csv` would corrupt any
"measured vs PHREEQC" comparison and any future ML correction layer with data we
did not measure. The run manager enforces this: a literature run can only write to
`literature_benchmark.csv`, never to a lab run's `experimental_release.csv`.

## Feeding a lab run into the existing pipeline

A lab run's data stays inside its own folder. To run the existing scripts against
it, use the app's **"Export to pipeline"** button (or
`run_manager.export_lab_run_to_pipeline`), which copies the run's
`experimental_release.csv` to
`data/raw/experimental_icp/experimental_release_manual_entry.csv` — the file the
existing scripts already read. No script changes are required.

## Git / data safety

Run **data and outputs are gitignored by default** (`experiments/*/data/*.csv`,
`experiments/*/outputs/`). Only this `README.md` is tracked. Do not commit real
lab data, literature datasets copied from papers, or generated outputs unless
explicitly approved.
