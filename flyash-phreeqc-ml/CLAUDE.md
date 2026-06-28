# CLAUDE.md — WPI Materials Research Assistant

## Project identity

This project is an AI-assisted materials research platform for fly ash / red mud / waste-material reuse research.

Describe it as:

AI-assisted materials research platform — private beta / professor-demo version.

Do not describe it as a fully validated public prediction platform.

## Core scientific rules

* AI does not directly simulate scientific results.
* AI helps understand prompts, route workflows, critique assumptions, explain outputs, and prepare inputs.
* PHREEQC runs geochemical simulations.
* ML surrogate models predict properties only when trained on approved data.
* Simulation is not validation.
* Validation requires measured data comparison.
* Do not fabricate composition, release fractions, measured data, model outputs, or validation status.
* Always distinguish assumptions, simulations, predictions, measured data, and validation.

## App architecture

Main systems:

* Assistant / AI Council
* PHREEQC engine
* Evidence / Literature Library
* ML Surrogate Engine
* Validation Layer
* Virtual LAB Machines (backend-only blueprint + runner)
* Settings / diagnostics

PHREEQC should run only after review and explicit confirmation.

Workflow lifecycle:
missing_inputs → ready_for_review → awaiting_confirmation → executed / failed

Recent work (implemented, on main):

* Demo workflow state/results stabilization (PR #25).
* Safe live-AI diagnostics + classified, sanitized fallback reasons (PR #26/#27).
* Chat → material-state sync (PR #28).

Chat → material-state sync (shipped — keep these invariants):
The Assistant and the Advanced details now share one canonical state (AgentState). Chat-typed
composition / release model / database are parsed deterministically (never AI-invented) by
`flyash_phreeqc_ml/agent/chat_setup_parser.py` and the deterministic PHREEQC builder reads the
same state. Invariants to preserve:

* Chat-typed composition is parsed and auto-fills Advanced details → Material composition.
* The parsed profile is draft/unconfirmed until the user confirms it (UI or chat).
* Once confirmed, the PHREEQC builder no longer reports needs_material_composition.
* Release model and database also auto-fill from chat.
* Composition is never invented; PHREEQC never auto-runs; the confirmation gate stays.

In progress (feature branches; not yet on main):

* XRD Advisory v2 — four advisory XRD modes. Draft PR #33 (`feature/xrd-advisory-v2`).
* Virtual LAB Machines — backend-only machine catalogue + executable runner.
  On `feature/virtual-lab-machines` (draft PR → `feature/digital-lab-instruments`).

Virtual LAB Machines (backend-only — keep these invariants):
"Virtual LAB" gives users virtual scientific machines (PHREEQC, XRD, ICP, FTIR/Raman, SEM-EDS,
TGA/DSC, Mechanical Testing, ML Surrogate, Literature Evidence, Sustainability/Cost, Experimental
Design, Validation/Uncertainty) to ESTIMATE / simulate / process / screen / prioritise experiments
and reduce trial-and-error — never to replace experimental validation. Two layers, both in
`flyash_phreeqc_ml/instruments/`:

* `virtual_lab_machines.py` — the metadata BLUEPRINT (what each machine can do, needs, must never
  claim, and how a result is verified). `ui_activation_status` is backend-only for every machine.
* `virtual_lab_machine_runner.py` — the EXECUTABLE runner over user-provided inputs; every result
  carries the standard fields (machine_id, status, output_data_type, result_summary, results,
  warnings, missing_inputs, assumptions, provenance, validation_status,
  can_be_used_for_validation_claim).

Invariants to preserve:

* Backend-only: not imported by app.py, not rendered by any ui/ file, not exported from
  instruments/__init__.py; imports no Streamlit and runs no website code.
* Every output is labelled (user_provided_assumption / synthetic_demo_data / literature_evidence /
  measured_lab_data / simulated_model_estimate / ml_prediction / advisory_interpretation /
  validated_result).
* No fabrication: ICP / SEM-EDS / TGA-DSC / Mechanical process only supplied rows; XRD never invents
  peaks; FTIR matches user peaks to broad advisory regions only; Sustainability invents no factors.
* PHREEQC is preview/gate only in the runner — it never executes PHREEQC (executed / auto_run always
  False; execution stays on the existing confirmation-gated path).
* A `validated_result` (can_be_used_for_validation_claim=True) is possible ONLY from the Validation
  machine with measured data AND explicit criteria that are met; simulation / ML / literature are
  never validated on their own.
* ML predicts nothing without an approved trained model; the Literature engine never scrapes
  (incl. Google Scholar) and keeps evidence candidate/unreviewed with provenance until human review.

## Demo test input

Use this as the main manual test:

im leeching class c fli ash w naoh .5m 2g 10ml for 1hr room temp wanna ph ca si.
use synthetic demo composition sio2 34 al2o3 18 cao 24 fe2o3 7 mgo 5 na2o 2 k2o 1 so3 4 loi other 5.
use global 1 percent release for ca si al fe na k.
use phreeqc.dat and 25 C.

Expected:

* leaching setup parsed
* composition parsed and auto-filled into Advanced details
* release model parsed and auto-filled
* database parsed and selected
* user confirms composition/release
* preview becomes ready_for_review
* user confirms run
* PHREEQC executes
* results appear in Assistant and Results tab

## Git safety

Before changes:

* show git branch
* show git status

Do not stage:

* flyash-lab-data-pipeline files
* raw data
* generated outputs
* Docker logs
* .env
* secrets
* .streamlit/secrets.toml
* model artifacts
* PHREEQC .pqi/.pqo/.sel outputs
* databases

Do not commit unless user explicitly asks.

Run before reporting:
python -m compileall -q app.py flyash_phreeqc_ml scripts ui
python -m pytest

## Secrets

Never print, store, commit, or log API keys.
Only report:

* key_present True/False
* key_length integer
* SDK available True/False
