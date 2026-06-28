# Virtual LAB — Machines layer (backend-only)

> **Status: backend-only metadata catalogue. Not wired into the website.** This document describes
> `flyash_phreeqc_ml/instruments/virtual_lab_machines.py`. Nothing here runs PHREEQC, calls an API,
> imports Streamlit, or activates any machine in the live Streamlit app. Every machine's
> `ui_activation_status` is `not_activated_backend_only`.

## Purpose

Virtual LAB gives researchers a mini virtual lab of scientific *machines* to produce **estimates,
simulations, screening, data processing, and experiment prioritisation** — so they can reduce
trial-and-error and decide which *few physical experiments* are worth doing. **It never claims to
replace real experimental validation.**

This module is the honest **capability catalogue**: for each machine it declares what it can do, the
inputs it needs, how its output must be labelled, what it must never claim, and how a result would be
verified in the real world. It is pure, import-safe data + small query/audit helpers.

## The honesty model

Every output must carry exactly one `output_data_type` label:

| label | meaning |
|---|---|
| `user_provided_assumption` | a value the user supplied / assumed |
| `synthetic_demo_data` | clearly-labelled synthetic demo data |
| `literature_evidence` | sourced literature (with provenance) |
| `measured_lab_data` | real measured laboratory data |
| `simulated_model_estimate` | output of a physical simulation (e.g. PHREEQC) |
| `ml_prediction` | output of a trained surrogate model |
| `advisory_interpretation` | an advisory reading / planning aid |
| `validated_result` | a measured-vs-model comparison that meets acceptance — **measured data required** |

The single hard gate: **a `validated_result` is only ever possible with measured data.** This is
enforced by `machine_can_produce_validated_result(machine_id, has_measured_data)` and re-checked by
`audit_virtual_lab_machines()`. Simulation is not validation; advisory interpretation is not
validation; literature evidence is not validation; ML prediction is not validation unless compared
against measured lab data.

## Machine schema (`VirtualLabMachine`)

`machine_id`, `display_name`, `short_description`, `category`, `mode`, `execution_mode`, `status`,
`what_it_can_do`, `required_inputs`, `optional_inputs`, `honest_outputs`, `output_data_type`,
`verification_required`, `real_world_verification_method`, `must_not_claim`, `needs_measured_data`,
`needs_trained_model`, `needs_reference_database`, `can_run_live`,
`should_use_cached_or_precomputed_data`, `uncertainty_controls`, `safety_notes`,
`example_user_prompts`, `future_backend_dependencies`, `ui_activation_status`.

**Allowed `mode`:** `physical_simulation`, `data_processing`, `advisory_planning`,
`trained_model_prediction`, `evidence_engine`, `cross_cutting_validation`.

**Allowed `execution_mode`:** `advisory_only`, `data_processing`, `preview_then_confirm`,
`trained_model_required`, `evidence_required`, `measured_data_required`.

**Allowed `status`:** `active_existing`, `phase_1_advisory`, `blueprint_only`,
`requires_reference_data`, `requires_trained_model`, `requires_measured_data`.

## The 12 machines

| # | Machine | mode | execution_mode | status | honest output |
|---|---|---|---|---|---|
| 1 | PHREEQC Leaching Simulator | physical_simulation | preview_then_confirm | active_existing | simulated_model_estimate |
| 2 | XRD Advisory / Pattern Planning | advisory_planning | advisory_only | phase_1_advisory | advisory_interpretation |
| 3 | ICP-OES / ICP-MS Data Processor | data_processing | data_processing | active_existing | measured_lab_data + advisory |
| 4 | FTIR / Raman Interpreter | advisory_planning | evidence_required | blueprint_only | advisory_interpretation |
| 5 | SEM-EDS Processor | data_processing | measured_data_required | requires_measured_data | measured_lab_data + advisory |
| 6 | TGA / DSC Processor | data_processing | measured_data_required | requires_measured_data | measured_lab_data + advisory |
| 7 | Mechanical Testing Processor | data_processing | measured_data_required | requires_measured_data | measured_lab_data |
| 8 | ML Surrogate Predictor | trained_model_prediction | trained_model_required | requires_trained_model | ml_prediction |
| 9 | Literature Evidence Engine | evidence_engine | evidence_required | blueprint_only | literature_evidence |
| 10 | Sustainability / Cost Screening | advisory_planning | advisory_only | blueprint_only | advisory + user_assumption |
| 11 | Experimental Design Assistant | advisory_planning | advisory_only | blueprint_only | advisory_interpretation |
| 12 | Validation & Uncertainty Assistant | cross_cutting_validation | measured_data_required | requires_measured_data | advisory **or** validated_result (measured only) |

## Helper functions

- `list_virtual_lab_machines()` — all machines, in catalogue order.
- `get_virtual_lab_machine(machine_id)` — one machine or `None`.
- `list_machines_by_mode(mode)` / `list_machines_by_status(status)` — filtered tuples.
- `machine_requires_measured_data(machine_id)` / `machine_requires_trained_model(machine_id)` /
  `machine_requires_reference_database(machine_id)` — capability gates.
- `machine_can_produce_validated_result(machine_id, has_measured_data)` — the validation gate.
- `audit_virtual_lab_machines()` — returns a list of completeness/safety problems (empty == healthy).

## What the audit enforces

Unique ids; all required fields present; `must_not_claim` / `safety_notes` / `verification_required`
/ `real_world_verification_method` non-empty for every machine; valid enum values; a `validated_result`
is impossible without measured data; ML requires a trained model; the Literature engine requires
provenance + human review and forbids scraping restricted sources (e.g. Google Scholar); Sustainability
screening is advisory / order-of-magnitude; ICP cannot fabricate measured data; XRD states the
formula-only / polymorph limitation.

## Not wired in (by design)

This layer is **backend-only**. It is not imported by `app.py`, no `ui/` file renders it, and it does
not change the live website. Activation into the UI is a separate, later step.
