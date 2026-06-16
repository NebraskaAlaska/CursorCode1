# Running PHREEQC from the Simulate tab (Step 9)

After you review a PHREEQC **input preview** (Step 8), the Simulate tab can **run** PHREEQC on
that exact input and show the basic predicted outputs (**Step 9 — Run deterministic model**).
This is the first layer that actually executes a model, and it is deliberately gated and kept
off the scientific result path.

## Planning vs. input preview vs. execution vs. validation

These are four distinct stages — keeping them separate is what keeps the app honest:

| Stage | What it is | What it produces | Trust |
| --- | --- | --- | --- |
| **Planning** (Steps 1–6) | AI/rule extraction of a scenario → a plan matrix | A *plan*, labelled "plan only" | a structured intent, not a prediction |
| **Input preview** (Step 8) | deterministic templating of a draft `.pqi` | reviewable input *text* | a draft input, nothing has run |
| **Execution** (Step 9) | running PHREEQC on the reviewed input | predicted pH / totals / saturation indices | a **simulation result**, *not* validated |
| **Validation** (Validate / Compare) | comparing **measured** data to model predictions | residuals + a validation status | the only stage that can say "validated" |

A Step-9 result tells you *what the model predicts under your assumptions*. It does **not** tell
you whether the model is right — that requires measured data and the mapping/residual workflow in
the **Validate** and **Compare Results** tabs.

## Safety properties

- **Never automatic.** Execution happens only when you tick the confirmation box and press
  **Run PHREEQC**. Nothing runs after AI parsing or preview generation.
- **AI never writes or modifies the input.** The executor runs the *exact* reviewed input text;
  the LLM only extracted the scenario you reviewed.
- **Off the result path.** A Step-9 result never changes mapping status, residuals, validation
  status, the comparison CSVs, or measured data. The executor imports no AI module and no
  comparison/residual/mapping module (pinned by `tests/test_ai_boundary.py`).
- **Never crashes the app.** A missing binary, a failed run, or a timeout returns a structured
  status (`phreeqc_missing` / `failed` / `timeout`), never an exception.
- **Clearly labelled.** Every result carries: *"Generated from PHREEQC execution of the reviewed
  simulation input. Not validated against measured data."*

## Configuring PHREEQC

The PHREEQC binary and the thermodynamic database are **user-supplied and never committed** (the
CEMDATA18 database is not redistributable). Set two environment variables:

```bash
export PHREEQC_EXE=phreeqc                          # the PHREEQC CLI binary (or put it on PATH)
export PHREEQC_DATABASE=/path/to/CEMDATA18-xx.dat   # your database file (not shipped)
export PHREEQC_TIMEOUT_S=120                         # optional hard timeout (default 120 s)
```

If either is missing, Step 9 shows:

> **PHREEQC execution is not configured.** You can still review and download the input preview.

The planner, preview, and `.pqi` download all work fully without PHREEQC installed — only Step 9
needs it. (This is the same configuration the Match-tab on-demand runner uses, so the two agree.)

## Where generated simulation files go

All execution artifacts are written to a **safe workspace**:

```
outputs/simulations/<scenario_id>.pqi      # the input that was run (verbatim)
outputs/simulations/<scenario_id>.pqo      # the PHREEQC output
outputs/simulations/<scenario_id>.sel      # a SELECTED_OUTPUT table, if PHREEQC produced one
```

The executor **refuses** to write into `data/raw`, `data/processed`, or the package source tree
(a guard raises before any file is created). The whole `outputs/simulations/` directory is
**gitignored** (`tests/test_phreeqc_executor.py::test_gitignore_protects_simulation_outputs` checks
this with `git check-ignore`), so generated `.pqi`/`.pqo`/`.sel` files are never committed.

## What Step 9 parses and shows

From the `.pqo` output (reliable) — plus the optional SELECTED_OUTPUT table when produced — the
basic parser extracts:

- predicted **pH** and **pe**;
- predicted **dissolved element totals** (converted molality → mM);
- **saturation indices** for the modelled phases.

It reports a parse status — `parsed` / `partial` / `no_selected_output` / `parse_failed` — and
states explicitly what was *not* available. A small bar chart of the predicted dissolved totals is
drawn **only** because an actual execution result exists; it is labelled a simulation output, not a
measurement.

## Why these results are not validation results

A simulation result is the model's prediction under the assumptions baked into the input (the
database, the dissolved-material composition, equilibrium-only chemistry, the candidate phase list).
None of that has been checked against your sample. To make a validation claim you still need:

1. **measured data** for the condition, and
2. a **mapping** from that measured condition to the model prediction, and
3. the **residual / inclusion** workflow that decides whether the comparison is `valid`.

That is why the pH and residual graphs in **Validate** and **Compare Results** are **separate** from
Step 9: they are driven by measured data + model predictions, never by a Simulate execution. A
Step-9 run changes none of them.

## Implementation

- `flyash_phreeqc_ml/simulation/phreeqc_executor.py` — `check_availability` / `smoke_test`,
  `execute_preview` (structured `ExecutionResult`, never raises), `parse_outputs` (structured
  `ParsedSimulation`), and the safe-workspace guard. Imports only `config` + the parsers; no AI, no
  comparison module, not even the Match-tab runner.
- The Simulate **Step 9** UI lives in `app.py` (`_render_run_deterministic_model` /
  `_render_simulation_result`).
- Covered by `tests/test_phreeqc_executor.py`; boundaries by `tests/test_ai_boundary.py`.
