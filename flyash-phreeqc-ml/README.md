# flyash-phreeqc-ml — Materials Research Assistant

A working **research prototype**: a **chatbot-style materials research assistant**. Describe a
materials experiment in plain language; the assistant asks for the missing details, identifies the
right modelling pathway, **runs available simulations after you confirm**, and helps **compare
predictions with measured data**. It combines three things usually kept apart: an **AI agent** that
plans and explains, **deterministic simulation engines** that do the chemistry, and a
**measured-data validation** workflow with full provenance.

**PHREEQC is the first executable engine, not the whole app.** It runs **leaching / geochemistry
(aqueous dissolution)** scenarios today. Other domains — polymer/composite mechanical testing,
thermal treatment, cementitious binders, battery/corrosion materials — get **planning + data-template
support** (structure the experiment, suggest response variables, build a dataset) and explicitly do
**not** pretend to simulate. More engines (literature RAG, surrogate ML, atomistic, mechanical-property
models) can be added modularly.

> The package keeps the historical `flyash-phreeqc-ml` slug. **Class C fly ash (+ metakaolin)
> alkali-activation modelled with PHREEQC and validated against experimental ICP** is the
> current, best-developed **example** — the strongest workflow, not the product's identity (see
> *Supported datasets & models*).

## Current identity & what's strongest

- **Identity:** a broad **Materials Research Assistant** — a conversational front door (the
  **Assistant** tab) over deterministic simulation + validation tooling. PHREEQC leaching is the
  first executable engine; everything else is planning-only for now, by design.
- **Strongest executable + validation workflow:** the **Class C fly ash + PHREEQC** leaching demo —
  measured ICP data → mapping → residuals → mapping-status → one honest validity line. This is the
  part with the most machinery and tests, and it is an *example of* the platform, not its whole scope.

## What the app can do today

- **Talk to a research assistant (Assistant Mode — the default workflow)** — describe your
  experiment conversationally; the assistant asks for the missing critical details, plans the
  modelling route, and — **only after you explicitly confirm** — runs the deterministic tools and
  explains the estimate and its limits. The AI handles conversation, clarification, planning, and
  explanation; **all chemistry stays deterministic and user-confirmed** (see *Assistant Mode*).
- **Plan a simulation from plain language** — AI (with consent) or a deterministic rule-based
  fallback extracts a structured scenario; **code, not the AI**, computes the missing-field and
  scientific caveats. You review, edit, and confirm before anything is built.
- **Build a reviewable PHREEQC input** — a material **composition profile** + a **release model**
  (how much dissolves) + a reviewed **candidate-phase template** → a deterministic draft `.pqi`
  you can read and download. **The AI never writes PHREEQC input.**
- **Check database compatibility** — see which phases your configured thermodynamic database
  actually defines (only available phases are added; missing ones are flagged, never invented).
- **Run PHREEQC** on the reviewed input — a single scenario or a small, capped parameter **sweep**
  — on an **explicit, gated click**; plot predicted **pH / element totals** vs the swept parameter.
- **Rank / target-match** executed results — rank candidates against an objective, or run an
  **inverse target search** ("get pH 10–12 while keeping Fe < 0.1 mM") over a small reviewed grid.
- **Validate against measured data** — map measured samples to model predictions, compute
  `residual = measured − model`, and read an honest **inclusion / validity** summary with
  per-variable counts and an excluded-rows table.
- **Save provenance & export** — every simulation run and every validation comparison is saved
  with a full provenance chain (inputs, assumptions, database/executable paths, hashes) and can be
  exported as a self-contained report.

## What it cannot (yet) claim

A model run is a **prediction under your assumptions** — it is **not validated** until compared to
measured data. No real measured fly-ash release data exists in the repo yet (only the blank
template), so for fly ash the validation workflow is **scaffolding awaiting data**, and **no ML is
trained**. See **[Limitations](#limitations-read-this-before-presenting)** — it is short and
important.

## The Research Assistant (the main workspace)

The **Research Assistant** is the product's main workspace — a clean, two-column chat that turns
the whole workflow into a conversation: the chat (history + input + actions) on the left, a
card-based status panel on the right (**experiment summary · domain / engine · missing details ·
current assumptions · next recommended action**). Technical content (scenario JSON, policy
decision, generated PHREEQC input, database report, material release model, raw result table,
provenance trace) stays **hidden under expanders**, so the default view is simple and
conversational. It is an **AI-agent orchestration layer** (`flyash_phreeqc_ml/agent/`) wrapped
around the *same* deterministic backend — the AI never does chemistry. The manual simulation
controls and the measured-data workflow live in the other sections (Advanced Mode / Data &
Validation), so nothing technical competes with the assistant.

- **The AI's role:** conversation, clarification (asking for the missing critical details),
  planning (choosing the modelling route), tool **orchestration** (proposing one action at a time),
  and **explanation** of results in plain language.
- **The deterministic backend's role:** every scientific calculation and the PHREEQC execution —
  scenario merge, the input-preview builder, the database/phase check, the gated executor, the
  ranking / target-matching layers, and the run registry. These are the existing, tested modules.
- **The policy layer:** a strict gate between *proposing* and *running*. **Execution and saving
  always require your explicit confirmation** (the model proposing a run never runs it — it is
  parked for a "Yes, run it" click or reply). PHREEQC is **blocked for non-leaching domains**, and
  a run is **blocked when required fields are missing** or no confirmed material composition exists.
- **Current executable engine:** PHREEQC, for **leaching / geochemical (aqueous dissolution)**
  scenarios only.
- **Unsupported domains are planning-only — and useful, not a dead-end.** For polymer/composite
  strength, thermal treatment, mechanical testing, corrosion/durability, battery materials, and a
  cementitious binder not framed as leaching, the assistant **does not pretend to simulate**. Instead
  it offers to **structure the experiment**, **build a data template**, and **identify the missing
  variables**, and it suggests the domain's **response variables** (e.g. for a composite: compressive
  strength, flexural strength, density, water absorption, toughness) and the inputs a **future model**
  would need — so you can build a dataset now. Future engines (literature RAG, surrogate ML,
  atomistic, mechanical-property models) can be added modularly.
- **Simulation is not validation.** Every estimate the assistant explains carries the standing
  "model estimate under reviewed assumptions — not measured, not validated" caveat, and measured ICP
  / pH data remain necessary to validate.
- **AI is opt-in.** With no API key the **deterministic planner** drives the same conversation
  (rule-based phrasing). The conversation is session-only; nothing AI-touched is saved without your
  confirm-gated save, and a saved run stores the **transcript summary + action trace + confirmed
  assumptions** — never the raw model response, secrets, or measured data. (Boundaries pinned by
  `tests/test_ai_boundary.py`; behaviour by `tests/test_agent.py`. Details:
  [`docs/assistant_agent.md`](docs/assistant_agent.md).)
- **Future architecture.** The orchestration is intentionally a simple, auditable
  *propose → policy-gate → confirm → deterministic tool* loop. It could be re-expressed with a
  graph-based agent framework (e.g. a LangGraph-style state machine) without changing the safety
  model — the policy gate, the confirmation requirement, and "AI never invents chemistry / never
  writes PHREEQC input" would still be the contract.

## Simulate vs. Validate / Compare (the key distinction)

These are deliberately separated, and the separation is enforced in code.

| | **Simulate** | **Validate / Compare** |
| --- | --- | --- |
| Question | "Given these inputs, what does the model predict?" | "Does the model agree with *measured* data?" |
| Inputs | a scenario + assumptions you choose | measured samples + a saved mapping |
| Output | a **simulation prediction** (PHREEQC output under assumptions) | residuals + a **validity status** |
| Can say "validated"? | **No** — never | **Only** when the comparison is valid (all plotted mappings exact, enough rows) |
| Touches measured data / residuals? | **Never** | yes — this *is* the result path |

A Simulate run never changes mapping, residuals, validation status, the comparison CSV, or the
Validate/Compare graphs. (Pinned by `tests/test_ai_boundary.py`.)

---

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # pandas / numpy / matplotlib / openpyxl / streamlit
                                       #   + OPTIONAL: anthropic (AI), scikit-learn/scipy (surrogate)
pip install -r requirements-dev.txt    # pytest (for the test suite)
```

The scripts and the app add the project root to `sys.path` themselves (see
`scripts/_path_setup.py`), so they run without installing the package. `pip install -e .` is
optional (for importing `flyash_phreeqc_ml` from notebooks).

## Launch the app

```bash
streamlit run app.py
```

The app opens on the **Research Assistant** — the main workspace. A clean sidebar holds run
management and a simple **four-section** navigation:

| Section | What's there |
| --- | --- |
| **Research Assistant** | the conversational workspace (chat + status cards); manual simulation lives here behind an **Advanced Mode** expander |
| **Projects / Runs** | saved runs, report export, audit trail, user guide |
| **Data & Validation** | the measured-vs-model workflow as sub-tabs: **Import · Validate · Match · Compare** |
| **Engine Settings** | engine roadmap, PHREEQC status, AI provider/model, future engine architecture |

No technical tab competes with the assistant — the Research Assistant is the product, and every
advanced workflow is still reachable (none was removed). The design is intentionally minimal
(Apple/Squarespace-inspired: light neutral background, white cards, restrained accent).

## Configure AI safely (optional, off by default)

The AI helpers (import suggestions, a grounded Q&A assistant, sourced literature retrieval, the
NL scenario parser) are **opt-in** — the app runs fully without them. Enable by installing the
`anthropic` SDK and providing a key via **either** the environment **or** a Streamlit secret
(environment wins if both are set):

```bash
export ANTHROPIC_API_KEY=sk-ant-...    # never commit this
export ANTHROPIC_MODEL=...             # optional model override
streamlit run app.py
```

The **Engine Settings** section's **AI** panel shows status (enabled, provider, model,
key-detected yes/no — **never the key**) and lets you pick the provider/model. AI is **suggestion /
interpretation only**: it cannot change mapping,
residuals, validation status, or the comparison, **cannot validate the science by itself**, and
**never writes PHREEQC input or invents target values**. No key is hard-coded, shown, or logged.
Full setup + precedence: [`docs/ai_configuration.md`](docs/ai_configuration.md).

## Configure PHREEQC safely (optional)

The planning, preview, and download steps work with **no** PHREEQC installed. To actually **run**
a model (Simulate Step 9+), point the app at a PHREEQC CLI you supply:

```bash
export PHREEQC_EXE=phreeqc                          # the phreeqc binary, or an absolute path
export PHREEQC_DATABASE=/path/to/your.dat           # a thermodynamic database (see below)
export PHREEQC_TIMEOUT_S=120                         # optional per-run timeout (default 120 s)
```

Execution is **gated** (it runs only on an explicit, confirmed click) and writes only to a safe,
gitignored workspace (`outputs/simulations/`) — never to `data/raw`, `data/processed`, or the
source tree. Details: [`docs/phreeqc_execution.md`](docs/phreeqc_execution.md).

### Configure CEMDATA18 (if you have it locally)

The thermodynamic **database controls which minerals/phases PHREEQC can predict**:

- **`phreeqc.dat`** (ships with PHREEQC) is fine for a **technical smoke test** — it runs and
  speciates Na/Ca/Si/Al/Fe and defines common minerals (Calcite, Gibbsite, SiO2(a), Gypsum…). But
  it is **weak for high-pH cementitious systems**: it lacks Portlandite, Ettringite, C-S-H, so a
  fly-ash alkaline-activation prediction is under-constrained.
- **CEMDATA18** is the cement-chemistry database designed for these systems. It is **not
  redistributable**, so this project **never ships it**. If you have a licensed copy:

```bash
export PHREEQC_DATABASE=/path/to/CEMDATA18-xx.dat
```

The Simulate **Database & candidate phases** step detects the database family and reports exactly
which template phases it defines (and which it skips). See
[`docs/database_compatibility.md`](docs/database_compatibility.md).

---

## 5-minute demo

A quick tour you can run **without** PHREEQC up to the input preview; steps marked **(needs
PHREEQC)** require `PHREEQC_EXE` + `PHREEQC_DATABASE` configured.

1. **Launch:** `streamlit run app.py`.
2. **Pick a demo run:** sidebar → **Experiment runs → ➕ Create new run** → choose
   `synthetic_demo` (clearly tagged as test data) → open the **Simulate** tab.
3. **Describe the experiment (Step 1–2):** e.g. *"2 g of Class C fly ash in 10 mL of 0.5 M NaOH
   for 60 min at room temperature; measure pH, Ca, Si, Al, Fe."* → **Parse scenario** → review the
   missing fields / assumptions / warnings → tick confirm.
4. **Generate the plan (Step 6):** choose *Single scenario* (or a *Small parameter sweep*) →
   **Generate simulation plan** (plan-only; nothing runs).
5. **Material profile (Step 7):** create a **test** composition profile (paste/enter oxide wt %)
   and **confirm** it. *This is a test assumption, not a measured assay.*
6. **Release model (Step 7b):** choose a **global release fraction** (e.g. 1 %). *Release fractions
   are user assumptions — they directly control the predicted dissolved totals.*
7. **Database & phases (Step 7c):** pick a phase template and read the compatibility report.
8. **PHREEQC input preview (Step 8):** generate + download the draft `.pqi`. **Still nothing runs.**
9. **Run one simulation (Step 9 — needs PHREEQC):** confirm, then **Run PHREEQC**; view pH / pe /
   element totals — labelled a **simulation output, not validated**.
10. **Run a small sweep (needs PHREEQC):** sweep e.g. NaOH concentration over a few values → view
    **pH / element-vs-parameter plots**.
11. **Rank / target-match (needs PHREEQC):** rank the swept results against an objective, or use
    **Step 10 — Target matching** to inverse-search (e.g. *maximise Si while keeping Fe < 0.1 mM*).
12. **Save / export:** **Save simulation run** (full provenance) and download the run package.

> Throughout the demo, the **test profile and release fractions are assumptions, not measured
> truth**, and every model output is labelled a prediction — **not** validated. Validation needs
> measured data and the **Compare** tab.

---

## For an advisor / professor

- **Why it's useful.** It turns a sentence-level experiment idea into a *transparent, runnable*
  geochemical model and a reproducible validation, lowering the barrier from "what should I
  expect?" to a sourced, auditable prediction — and it makes the model's **uncertainty and
  assumptions explicit** rather than hidden.
- **Why PHREEQC + measured data + AI planning together.** PHREEQC supplies the **mechanistic
  chemistry** (speciation, saturation, precipitation); measured ICP data is the **ground truth**;
  AI only **lowers the friction** of going from prose to a structured scenario. Each does what it
  is good at, and none is trusted outside its lane.
- **Why validation is separated from simulation.** A model that hasn't been checked against
  measurement is a hypothesis. The app **refuses to call a simulation "validated"**: simulation
  outputs and validation results are different artifacts, in different tabs, with different
  honesty wording — so a confident-looking plot can never be mistaken for a verified result.
- **Why provenance matters.** Every prediction is traceable to its experiment text → scenario →
  assumptions → material profile → generated input → executable/database → output → parser status.
  A literature value can only enter a calculation after **explicit confirmation**, and its DOI is
  kept. This is what makes the workflow defensible in a research setting.
- **How this supports future experimental design.** The inverse **target-matching** and the
  "**conditions needing new simulations**" / "under-attributed conditions" surfaces point at which
  experiments would be most informative next — a bridge from modelling to a prioritized bench plan.

---

## Limitations (read this before presenting)

- **Release fractions are user assumptions** unless explicitly measured or
  literature-confirmed — they are the single biggest lever on predicted dissolved totals. The app
  never defaults to 100 % dissolution and labels every non-measured fraction as assumed.
- **The PHREEQC database controls mineral/phase predictions.** Different databases give different
  precipitation and saturation-index results from the *same* input.
- **`phreeqc.dat` is fine for a smoke test but weak for cementitious high-pH systems** (no
  Portlandite/Ettringite/C-S-H). High-pH fly-ash chemistry needs **CEMDATA18**.
- **CEMDATA18 is not shipped** (not redistributable) — you supply it locally.
- **Simulation outputs are not validated** until compared with measured / lab data in the
  **Compare** tab. A near-zero residual only indicates agreement **if the mapping is
  scientifically valid**.
- **Kinetic dissolution is future work** — the source term is an **equilibrium** model, not a
  rate model.
- **Large-scale / adaptive search is future work** — Simulate supports small, reviewed, **capped**
  sweeps and grids only; global optimisation needs a dedicated backend or surrogate.
- **No ML is trained on the result path.** The experimental surrogate / bias / correction layers
  are display-only and hard-gated; for fly ash they have **no data to train on yet**.

---

## Data safety & generated outputs

**Do not commit** secrets, real raw research data, or generated artifacts. Specifics:

- **Secrets:** never commit `ANTHROPIC_API_KEY`, `.env`, or `.streamlit/secrets.toml`. The key is
  read from the environment / Streamlit secrets only and is never shown or logged.
- **Proprietary databases:** never commit CEMDATA18 or any licensed `.dat`.
- **Measured / lab data:** measured-release CSVs, mapping CSVs, and run data are **gitignored by
  default**; only the blank `experimental_release_template.csv` is tracked. Do not force-add real
  data without approval. A tracked **pre-commit hook** (`scripts/hooks/pre-commit`) blocks staged
  spreadsheets, measured/release CSVs, and generated outputs — install it with
  `cp scripts/hooks/pre-commit ../.git/hooks/pre-commit && chmod +x ../.git/hooks/pre-commit`
  (run from this directory; this project is a subfolder of the parent git repo).

**Generated-output folders (all gitignored, all re-creatable):**

| Folder | What | Created by |
| --- | --- | --- |
| `data/processed/` | parsed/processed CSVs (`phreeqc_results.csv`, comparison, …) | the scripts |
| `reports/figures/` · `outputs/figures/` | exploratory + result plots | the scripts |
| `outputs/tables/` | validation + sustainability tables | scripts 07/08 |
| `outputs/simulations/` | Simulate execution workspace — generated `.pqi`/`.pqo`/`.sel` | the executor |
| `outputs/simulation_runs/` | saved Simulate run provenance bundles | the run registry |
| `experiments/<run>/data/`, `experiments/<run>/outputs/`, `run_config.yaml` | per-run save files + comparison artifacts | the run manager |

Only `experiments/README.md` is tracked under `experiments/`.

---

## Architecture (developer note)

The app was modularized so it stays maintainable; the layering is enforced by tests.

- **`app.py` is a thin entry point** (~210 code lines): page config + hero, the run-management
  sidebar + the four-section nav, and inline dispatch to `ui.<module>.render(...)`. No workflow
  logic lives here.
- **`ui/` is the UI layer** — one module per workflow/section (`ui/<name>_tab.py` + `ui/engine_settings.py`,
  each exposing `render`) plus shared state (`ui/state.py`), shared render helpers (`ui/common.py`),
  pure formatters (`ui/formatters.py`), and the presentation/design system (`app_ui.py`, with the
  base palette in `.streamlit/config.toml`). The Research Assistant (`ui/assistant_tab.py`) is the
  main workspace; the import graph is an acyclic DAG (workflows → state/common/formatters).
- **`flyash_phreeqc_ml/simulation/` holds the scientific simulation logic** — scenario schema,
  rule parser, plan matrix, **source terms** (release model), **database compatibility** + **phase
  templates**, the deterministic **input builder**, the gated **executor** + **batch executor**,
  **strategy** (ranking) and **target matching**, and the **run registry**.
- **The validation / result-path modules are separate and unchanged** — `compare/`, `scenarios`,
  `replicates`, `mapping_table`, `mass_balance`, `attribution`, `viz/`. They compute mapping,
  residuals, and validity, and never import the UI or AI.
- **AI modules (`flyash_phreeqc_ml/ai/`) are suggestion-only** and **off the scientific result
  path** — they never compute mapping/residuals/validity and never write PHREEQC input.
- **`flyash_phreeqc_ml/agent/` is the AI-agent orchestration layer** behind the Assistant tab.
  It wraps an LLM around the deterministic Simulate backend: per turn the model **proposes one
  structured action**, a **policy layer** gates it (execution/save require explicit confirmation;
  PHREEQC is blocked for non-leaching domains and missing-composition runs), and a **tool registry**
  runs the existing deterministic functions. Its pure modules (`agent_state` / `agent_actions` /
  `agent_prompts` / `agent_policy` / `domains`) import no AI and no executor; only the orchestrator
  touches AI; only the tool registry touches the executor; and **no scientific/result-path module
  imports the agent**. With no key, a deterministic planner drives the same flow.
- **Generated outputs stay under `outputs/`** (and `data/processed/`, `experiments/<run>/`), all
  gitignored. The scientific package never imports `ui`/`app` — the dependency arrow points one
  way: `app.py → ui/ → flyash_phreeqc_ml/`.

Full architecture + "how to add a UI section safely": [`docs/refactor_plan.md`](docs/refactor_plan.md).
Boundaries are pinned by `tests/test_ui_modularization.py` and `tests/test_ai_boundary.py`.

---

## Project phases

| Phase | Scope | Status |
|-------|-------|--------|
| **1. Parse + analyze** | Parsers for `.pqi`, `.pqo`, `SELECTED_OUTPUT`, ICP Excel/CSV → processed CSVs → `master_dataset.csv` → exploratory plots | ✅ implemented |
| **2. PHREEQC vs experiment** | Measured-release template + parser, residuals (measured − PHREEQC), measured-vs-model plots, inclusion/validity | 🟡 scaffolding ready (awaiting measured data) |
| **Simulate (forward planning + run)** | NL → scenario → plan → material/release/database → input preview → **gated PHREEQC run + sweep + plots** → ranking / target matching → saved provenance | 🟢 working (planning + gated execution; outputs are predictions, not validated) |
| 3. ML correction / surrogate | Models that learn *where the model disagrees with experiment* | ⬜ experimental scaffolding only (display-only, hard-gated; no training data yet) |

> **The long-term aim** is an ML **correction** layer that learns where PHREEQC disagrees with
> experiment — **not** a blind replacement for the chemistry. For fly ash, real measured release
> data does not exist in the repo yet, so Phase-2 comparison remains the scientific ceiling.

## Supported datasets & models (what "supported" means)

The comparison workflow — *measured data → model prediction → mapping → residuals → validation
status* — is **model-agnostic**: PHREEQC + fly-ash metadata are the current implementation, not a
hard limit. The claims below are exactly the ones pinned by the **supported-dataset matrix**
(`tests/matrix/`, one module per claim). The app supports what the matrix tests — no more.

| # | Supported shape | Claim pinned | Test module |
|---|-----------------|--------------|-------------|
| a | Fly-ash measured data + **PHREEQC** | end-to-end suggestion → mapping → inclusion, all four statuses | `test_a_flyash_phreeqc.py` |
| b | Literature-style fly-ash benchmark | kept a **separate run type**; literature can never enter a lab comparison | `test_b_literature_separation.py` |
| c | Synthetic measured + known model | residual = **measured − model**, exactly (sign + join correct) | `test_c_hand_residuals.py` |
| d | Reformatted upload (renamed/reordered cols, mg/L) | resolves via the importer + the **unit contract** with conversion provenance | `test_d_reformatted_import.py` |
| e | Alternate **non-fly-ash** dataset profile | grouping → suggestion → inclusion run from the profile alone | `test_e_alternate_profile.py` |
| f | **Non-PHREEQC** model via the prediction-CSV contract | mapped + compared end-to-end through the same manifest | `test_f_generic_prediction.py` |
| g | **Second material** (red mud) via a `MaterialProfile` | batch closure → (mocked) attribution → recovery, zero fly-ash leak | `test_g_second_material.py` |

A non-PHREEQC model supplies predictions through the documented **model-prediction CSV contract**
(`docs/model_prediction_format.md`). The Data tab's **"Import model predictions (CSV)"** path
ingests it, and `scenarios.build_scenario_manifest` consumes it exactly like PHREEQC output.

## Navigation — four sections

The sidebar navigates four sections (the **Research Assistant** is the default workspace; every
advanced workflow lives in one of the other three).

### Research Assistant (the workspace)
A chat with example prompt chips and a card-based status panel (experiment summary · domain /
engine · missing details · current assumptions · next recommended action). It asks for missing
details, plans the route, and — only after you confirm — runs the deterministic tools and explains
the estimate. For planning-only domains it shows a **planning-support** panel (suggested response
variables + a downloadable data template + plan/missing-variable actions) instead of dead-ending.
Technical detail (scenario JSON, policy decision, input preview, database report, release model,
result table, provenance trace) is tucked under expanders. The **Advanced Mode** expander holds the
full manual simulation workflow:

- **Advanced Simulate** — the forward-looking core (planning + gated execution): describe → AI/rule
  scenario → confirm → plan matrix → material profile → release model → database & phases → draft
  `.pqi` preview → **gated PHREEQC run + small sweep + plots** → ranking / refined sweep / target
  matching → save provenance. Plan generation runs nothing; execution is a separate confirmed step.

### Projects / Runs
Saved runs, **report export**, the **audit trail**, and the in-app user guide.

### Data & Validation
The rigorous measured-vs-model workflow, as sub-tabs:

- **Import** — run-type-specific entry: a **generic** `.csv`/`.xlsx`/`.xls` importer (sheet
  pick → fuzzy column mapping → unit conversion mg/L·ppm·ppb→mM → leachant/provenance → pre-save
  validation → confirm-gated save) and a special-case **Class C fly ash dissolution-workbook**
  parser; plus literature CSV upload, manual rows, row editing, and CSV/pipeline export.
- **Validate** — measured-data overview, data-quality validation, the **Calculation Verification**
  view (formula registry, per-row residual audit, calculators), and the model raw-outputs viewer.
- **Match** — replicate-aware guided mapping of measured data to model predictions (current model:
  PHREEQC): a Scenario Explorer (with the sol1/sol2/sol3 = replicate/batch explanation and the
  OA/PF/GS cup-cover caveat), condition-level mapping with replicate inheritance, a collision
  check, and the **"conditions needing new simulations"** table.
- **Compare** — run the pipeline, then read the **measured-vs-model comparison** (inclusion
  counts, residuals, systematic bias, the validity line; default replicate mean ± std), with the
  "workflow check, not final validation" warning unless mappings are exact, plus the grounded
  assistant and the experimental surrogate (display-only). **Simulation outputs are not validation
  until compared here** against measured data.

### Engine Settings
The **engines & capabilities** roadmap (available now / planning support now / future), the
**PHREEQC** executable/database status + how to configure it, the **AI** provider/model selector
and status (never the key), and the **future plugin-engine architecture** note (a LangGraph-style
orchestrator, RAG / ML / simulation / validation agents) — see
[`docs/ai_architecture.md`](docs/ai_architecture.md).

## Experiment runs / save files

The app keeps several independent experiments side by side, like **save files** — each in its own
`experiments/<safe_run_name>/` folder with a `run_config.yaml`, `data/`, and `outputs/`. This is an
app-level save/open layer over the existing pipeline. Create one in the sidebar (**Experiment
runs → ➕ Create new run**); see [`experiments/README.md`](experiments/README.md).

**Run types** decide which data file a run uses and how its data is treated:

| run_type               | data file                       | meaning                                              |
|------------------------|---------------------------------|------------------------------------------------------|
| `lab_experiment`       | `data/experimental_release.csv` | **real measured lab data**                           |
| `literature_benchmark` | `data/literature_benchmark.csv` | values **reported by other papers** (comparison only)|
| `synthetic_demo`       | `data/demo_data.csv`            | fake/demo data for testing (tagged synthetic)        |
| `plastic_composite`    | `data/experimental_release.csv` | plastic / fly-ash composite side project             |

**Why literature data stays separate.** Literature values are other people's results under other
people's conditions; mixing them into `experimental_release.csv` would corrupt any
measured-vs-model comparison. The run manager **enforces** this — a literature run can only write
`literature_benchmark.csv`, never a lab run's `experimental_release.csv`.

### Sample → model mapping (needed for residuals)

PHREEQC `.pqo` outputs and lab samples share no key, so each measured `sample_id` is linked by hand
to the model result row (`record_key`) that represents the *same* chemistry. Without the mapping,
the comparison leaves residuals `NaN` (a deliberate "not linked yet" state, not a wrong join). The
**Match** tab's Mapping Assistant scores scenarios with transparent rules and offers the top
candidates; the mapping is saved to the run's `data/sample_phreeqc_map.csv` and exported to the
pipeline for `scripts/05`. Mapping conventions: [`docs/mapping_rules.md`](docs/mapping_rules.md).

## Entering measured experimental data (Phase 2)

Lab/ICP results are entered against a fixed template so the parser, comparison, and tests agree on
the schema. **Copy the template** (`data/raw/experimental_icp/experimental_release_template.csv`)
to a new dated file in the same folder; keep the template blank. **Fill one row per sample** (extra
columns are allowed and preserved; leave unknown numeric cells **blank**, not `n/a`). Key columns:

| Column | Meaning / units |
|--------|-----------------|
| `sample_id` | unique id (links to the model) |
| `fly_ash_type` | e.g. `CFA`, `CFA+MK` |
| `NaOH_M`, `time_min`, `temperature_C`, `liquid_solid_ratio` | activator + process metadata |
| `CO2_condition` | cup-cover code: `OA` (open air) / `PF` (plastic flap) / `GS` (glass cover) / `atm_CO2` / `low_CO2` / `no_CO2` / `unknown` |
| `initial_pH`, `final_pH` | measured pH before/after |
| `Ca_mM`, `Si_mM`, `Al_mM`, `Fe_mM`, `Na_mM`, `K_mM` | measured concentrations (**mM**) |
| `Sc_ppb`, `total_REE_ppb` | trace concentrations (**ppb**) |

> **CO₂ cup-cover convention:** `OA/PF/GS` are CO₂-exposure cup covers — **OA = open air** (direct
> atmospheric CO₂), **PF = plastic flap**, **GS = glass cover**. PF/GS likely reduce CO₂ exchange
> but are **never** called "sealed" unless airtight sealing is experimentally confirmed.

Then run the comparison (only acts when measured data is present, otherwise it exits cleanly):

```bash
python scripts/01_parse_phreeqc.py        # ensure phreeqc_results.csv exists
python scripts/05_compare_experimental.py # measured vs PHREEQC + residuals + plots
```

> **Units:** PHREEQC reports element totals as molality (mol/kgw); the comparison multiplies by
> 1000 to get mM (`config.PHREEQC_MOLALITY_TO_MM`) so it matches the measured `*_mM` columns. For
> dilute solutions mol/kgw ≈ mol/L. **Fe** may stay `NaN` if the runs don't model Fe — that is
> "unavailable", not "predicted zero".

## Command-line pipeline (optional; the app wraps these)

```bash
# Phase 1 — parse → processed CSVs → master dataset → plots
python scripts/run_phase1.py
# Phase 2 — measured vs PHREEQC (no-op until measured data exists)
python scripts/05_compare_experimental.py
# Pre-data experiment planning + QA/QC (no ML)
python scripts/06_generate_experiment_plan.py     # -> data/raw/experimental_icp/experiment_plan.csv
python scripts/07_validate_experimental_data.py   # -> outputs/tables/experimental_validation_report.csv
python scripts/08_sustainability_score.py         # -> outputs/tables/sustainability_score.csv
# On-demand PHREEQC + surrogate (need a user-supplied PHREEQC CLI + database):
python scripts/09_generate_simulations.py --run "<run>"           # generate/run/ingest needs-new conditions
python scripts/10_sample_design.py --run "<run>" --n-samples 200  # LHS design -> surrogate dataset
```

## Project layout

```
flyash-phreeqc-ml/
├── app.py                          # THIN entry point: sidebar + tab dispatch only
├── ui/                             # UI layer (one module per tab + shared state/helpers)
│   ├── state.py · common.py · formatters.py
│   └── start_tab.py · simulate_tab.py · import_tab.py · validate_tab.py
│       · match_tab.py · compare_tab.py · export_tab.py
├── flyash_phreeqc_ml/              # the importable package (the science)
│   ├── config.py                   # all paths + domain constants in one place
│   ├── parsers/                    # .pqi / .pqo / SELECTED_OUTPUT / ICP parsers
│   ├── compare/                    # Phase 2: residuals + inclusion/validity
│   ├── scenarios.py · replicates.py · mapping_table.py   # mapping + suggestion (no ML)
│   ├── mass_balance.py · attribution.py · report.py      # closure / attribution / report
│   ├── units.py · calculations.py · audit.py · run_manager.py
│   ├── simulation/                 # Simulate scientific logic (scenario→preview→run→rank→target)
│   ├── materials/                  # material composition manager (profiles)
│   ├── ai/                         # OPTIONAL, suggestion-only (off the result path)
│   ├── ml/                         # experimental, display-only (surrogate / bias / correction)
│   └── viz/                        # Phase 1 + Phase 2 plots
├── scripts/                        # thin command-line entry points (01–10, run_phase1)
├── tests/                          # pytest suite (+ tests/matrix/ for the supported claims)
├── docs/                           # reference docs (see below)
├── data/{raw,processed}/ · reports/figures/ · outputs/ · experiments/
```

## Documentation

| Doc | Topic |
| --- | --- |
| [`docs/simulation_planner.md`](docs/simulation_planner.md) | the Simulate flow (Steps 1–10) |
| [`docs/material_profiles.md`](docs/material_profiles.md) · [`docs/material_release.md`](docs/material_release.md) | composition + release model |
| [`docs/database_compatibility.md`](docs/database_compatibility.md) | databases + candidate phases |
| [`docs/phreeqc_execution.md`](docs/phreeqc_execution.md) | the gated execution layer |
| [`docs/simulation_strategy.md`](docs/simulation_strategy.md) · [`docs/target_matching.md`](docs/target_matching.md) | ranking + inverse search |
| [`docs/simulation_runs.md`](docs/simulation_runs.md) | run provenance |
| [`docs/comparison_inclusion.md`](docs/comparison_inclusion.md) · [`docs/mapping_rules.md`](docs/mapping_rules.md) | validation inclusion + mapping rules |
| [`docs/mass_balance.md`](docs/mass_balance.md) · [`docs/defining_a_material.md`](docs/defining_a_material.md) | closure + new materials |
| [`docs/ai_configuration.md`](docs/ai_configuration.md) | AI setup + safety |
| [`docs/refactor_plan.md`](docs/refactor_plan.md) | UI architecture (developer note) |
| [`docs/user_guide/`](docs/user_guide/) | in-app user guide (also rendered in the Export tab) |

## Tests

```bash
pip install -r requirements-dev.txt   # pytest
python -m pytest
```

The suite covers the parsers and residual math, the run manager + mapping, the calculation/audit
registry, the scenario manifest + rule-based mapping, the simulation layer (planner, source terms,
database compatibility, executor, batch, strategy, target matching, run registry), the
supported-dataset matrix, and the architecture/AI boundaries
(`tests/test_ui_modularization.py`, `tests/test_ai_boundary.py`). Real PHREEQC integration tests
**skip** unless a PHREEQC binary + a compatible database are configured.
