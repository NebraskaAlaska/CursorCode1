# Simulation run registry (provenance for Simulate executions)

When you run PHREEQC from the Simulate tab (a [single scenario or a small sweep](phreeqc_execution.md)),
you can **save the run** with its full provenance. Every predicted value is then traceable back
through the whole chain:

> experiment text → parsed scenario → confirmed assumptions → material profile → generated PHREEQC
> input → executable / database used → output files → parser status → warnings

These saved runs are **simulation runs, not validation runs** — and the registry keeps them
strictly separate from measured-data validation.

## Simulation run vs. validation run

| | **Simulation run** (this registry) | **Validation run** (the run manager) |
| --- | --- | --- |
| What it captures | a PHREEQC execution of a reviewed input | measured data ↔ model-prediction mapping + residuals |
| Produces | predicted pH / totals / SIs under your assumptions | residuals + a validation status |
| Stored under | `outputs/simulation_runs/` (gitignored) | `experiments/<run>/` (the run manager) |
| Trust | a model prediction — **not validated** | the only place a "validated" claim can be made |
| Affects mapping/residuals/comparison? | **never** | yes — that *is* the validation workflow |

A simulation run records *what the model predicted*. It says nothing about whether the model is
right; that needs measured data and the Validate / Compare workflow.

## Where simulation outputs are stored

One folder per saved run, under `outputs/simulation_runs/<run_id>/`:

```
run_metadata.json          # provenance scalars + lists + per-scenario records
assumptions_warnings.json  # the assumptions and warnings (incl. a missing-profile warning)
scenario_matrix.csv        # the plan matrix that was run
parsed_results.csv         # the per-scenario parsed result table (pH / pe / totals / key SIs)
inputs/<scenario_id>.pqi   # the exact reviewed inputs, copied in for self-containment
```

The whole `outputs/simulation_runs/` tree is **gitignored** (a test asserts this with
`git check-ignore`). The registry **refuses** to write into `data/raw`, `data/processed`, or the
package source tree (the safe-path guard raises before any file is created), and it never writes to
a measured/validation CSV.

## What provenance is recorded

`run_metadata.json` carries: `run_id`, `created_at`, `user_label`, `notes`,
`original_experiment_text`, `desired_outputs_text`, `parser_source` (`ai` / `rule` /
`rule_fallback` / `manual`), the structured `scenario_json`, the `material_profile_summary` +
`material_profile_verification_status`, the `phreeqc_executable_path` / `phreeqc_database_path`
(paths, not contents), the `phreeqc_input_paths` / `phreeqc_output_paths`, the
`execution_status_summary`, the `plot_axis`, and a per-scenario `scenarios` + `outputs` list. The
assumptions and warnings live in `assumptions_warnings.json`; the matrix and the result table are
the two CSVs.

**No secrets are stored.** The API key is never exposed by the app and never written here, and the
**raw AI response is not persisted** (only the structured, reviewed scenario is). A test exports a
run package and asserts it contains no key-like strings and no `raw_response`.

**Honesty by construction.** Every record carries the label *"Simulation run — PHREEQC execution of
reviewed inputs. Not validated against measured data."*, and a **missing material profile is
recorded as a warning** (composition is never invented).

## Provenance in the UI

After an execution, the Simulate result table and plots carry a one-line **provenance caption**:
the saved run id (once saved), *source: PHREEQC execution of reviewed input*, the material profile
used, the parser source, and the executable / database — always ending with **"Not validated
against measured data."**

## How to reload / export simulation results

- **Save** — in the Simulate tab, after a run, open *Save simulation run*, add an optional label +
  notes, confirm, and save. You then get download buttons for `run_metadata.json`,
  `parsed_results.csv`, and a **run package** (a zip of the whole folder).
- **List / reload** — the **Export** tab has a *Previous simulation runs* table (run id, label,
  timestamp, number of scenarios, success/failure counts, material, leachant, sweep axis), with a
  per-run zip download. These are listed **separately** from validation runs.

## Why these are not measured data — and how they can be used later

A saved simulation run is a **model prediction**, generated under the assumptions baked into the
input (database, dissolved-material composition, equilibrium-only chemistry, the candidate-phase
list). It is **not** a measurement of any sample.

These records become scientifically useful **only after a measured comparison exists**:

- for **validation**, a saved prediction can be compared to measured data through the normal
  Validate / Compare mapping + residual workflow — at which point the residuals (not the raw
  prediction) carry the validation status;
- for **ML training** (e.g. a residual-correction model), only the *measured − model* residuals
  from an exact-mapped comparison may be used as targets — never the raw simulation outputs on their
  own. The hard data-sufficiency gates on the ML side (≥30 exact pairs / ≥3 conditions) still apply.

Until that measured comparison exists, a simulation run stays exactly what it is labelled: a
prediction, not a result.

## Implementation

- `flyash_phreeqc_ml/simulation/run_registry.py` — `SimulationRunRecord` /
  `SimulationScenarioRecord` / `SimulationOutputRecord`, `build_run_record`,
  `SimulationRunRegistry` (`save_run` / `list_runs` / `load_run` / `export_zip`), the safe-path
  guard (reusing the executor's), and the no-secrets JSON serialization. Imports only `config` +
  the executors (+ pandas/stdlib) — no AI, no comparison module.
- The Simulate save UI + provenance captions and the Export-tab list live in `app.py`.
- Covered by `tests/test_run_registry.py`; boundaries by `tests/test_ai_boundary.py`.
