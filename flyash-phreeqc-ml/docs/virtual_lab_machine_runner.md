# Virtual LAB — Machine runner (executable, backend-only)

> **Status: executable backend layer. Not wired into the website.** This documents
> `flyash_phreeqc_ml/instruments/virtual_lab_machine_runner.py`. It runs small, safe workflows over
> **user-provided** inputs. It does **not** import Streamlit, does **not** execute PHREEQC, does **not**
> call external APIs, and is not imported by `app.py` or any `ui/` file.

## What it is

The first *executable* layer on top of the machine blueprint
(`virtual_lab_machines.py`). `run_virtual_lab_machine(machine_id, payload, confirm=False)` dispatches to
a per-machine handler and returns a standard, self-describing `VirtualLabMachineResult`.

## The standard result

Every result carries: `machine_id`, `status`, `output_data_type`, `result_summary`, `results`,
`warnings`, `missing_inputs`, `assumptions`, `provenance`, `validation_status`,
`can_be_used_for_validation_claim`.

`output_data_type` is the honest epistemic label (reused from the blueprint). The hard gate:
`can_be_used_for_validation_claim` is `True` **only** for the Validation machine when measured data +
explicit criteria are present **and met** — every other result is an estimate / advisory / processed
data and is `False`.

## Public API

- `VirtualLabMachineRequest` / `VirtualLabMachineResult`
- `run_virtual_lab_machine(machine_id, payload, confirm=False)`
- `validate_machine_inputs(machine_id, payload)` — missing required inputs (or `['unknown machine_id']`)
- `explain_missing_inputs(machine_id, payload)` — human-readable "provide X — …" lines
- `get_machine_result_label(machine_id)` — the machine's primary honest output_data_type
- One `run_<machine>()` helper per machine (e.g. `run_phreeqc_leaching`, `run_icp_processor`, …)

## What each machine does now — and still refuses

| Machine | Does now | Still refuses |
|---|---|---|
| PHREEQC Leaching | validates inputs, builds an input **preview**, enforces preview→confirm | **executes nothing** (delegates to the existing gated engine); never claims validation |
| XRD Advisory | delegates to the repo's `xrd_advisory` (expected peaks / PHREEQC checklist); records measured peaks as **user-provided** | inventing peaks; identifying phases; matching measured peaks (no reference data this phase) |
| ICP Processor | delegates to `icp_processor` (mg/L→mM, blank/dilution, QC, residuals) over your rows | simulating the plasma; fabricating measured values; labelling assumptions "measured" |
| FTIR / Raman | matches **your** peaks to broad functional-group **regions** (advisory) | fabricating spectra; definitive compound identification; unmatched peaks → reference-data-needed |
| SEM-EDS | summarises **your** elemental rows (elements present, min/max/mean), flags missing standards | fabricating images/maps; inferring exact mineral phases from EDS |
| TGA / DSC | computes total mass loss / DSC extrema from **your** arrays | fabricating curves; definitive phase/reaction assignment |
| Mechanical | mean / std-dev / count from **your** measured strengths; flags <3 replicates | inventing strengths; claiming code/standard compliance |
| ML Surrogate | returns `trained_model_required` (no model wired this phase) | predicting / claiming accuracy or validation without a trained model |
| Literature | records **your** metadata rows as candidate, unreviewed, provenance-tracked | web scraping (incl. Google Scholar); treating evidence as reviewed truth |
| Sustainability | order-of-magnitude `amount × factor` from **your** assumptions | inventing factors; final LCA/TEA; certified carbon savings |
| Experimental Design | a deterministic plan (controls, replicates, factor matrix, measurement plan) | reporting any experimental result |
| Validation & Uncertainty | residuals / abs & % error from measured vs predicted; gated validated-result | calling anything validated without measured data **and** met criteria |

## Validation status values

`not_applicable`, `no_measured_data`, `comparison_available`, `insufficient_data`,
`validated_against_measured_data` (the last only with measured data + criteria that are met).

## Safety properties

- No fabrication: ICP / SEM-EDS / TGA-DSC / Mechanical process only supplied rows; XRD never invents
  peaks; FTIR matches only to broad advisory regions; Sustainability invents no factors.
- PHREEQC is preview/gate only — `executed` and `auto_run` are always `False` here.
- Simulation / advisory / literature / ML are never "validated" on their own — only a measured
  comparison meeting explicit criteria yields `validated_result`.
- Import-safe: the module imports only stdlib + the blueprint at top; `xrd_advisory` / `icp_processor`
  are lazy-imported inside their handlers. No Streamlit anywhere.

## Not wired in (by design)

Backend-only. Not imported by `app.py`, not rendered by any `ui/` file, not auto-registered. UI
activation is a separate, later step.
