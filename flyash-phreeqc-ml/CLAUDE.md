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
* Settings / diagnostics

PHREEQC should run only after review and explicit confirmation.

Workflow lifecycle:
missing_inputs → ready_for_review → awaiting_confirmation → executed / failed

Current debugging focus:
Synchronize Assistant-parsed material composition, release model, and database with the Advanced details UI state.

Problem to fix:
The Assistant can understand a composition from chat, but the deterministic PHREEQC builder still sees Advanced details → Material composition as empty. The Assistant and Advanced options must share one canonical state.

Desired behavior:

* If user types composition in chat, parse it.
* Auto-fill Advanced details → Material composition.
* Mark parsed profile as draft/unconfirmed.
* User can confirm via UI or chat.
* Once confirmed, PHREEQC builder should not say needs_material_composition.
* Release model and database should also auto-fill from chat.
* Do not auto-run PHREEQC.
* Keep confirmation gate.

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
