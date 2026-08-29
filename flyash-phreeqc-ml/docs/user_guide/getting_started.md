# Getting started

This app is an **AI-assisted platform for geochemical / material-leaching simulation and
validation**. You describe an experiment and the variables you want; it extracts a structured
scenario, flags missing info and assumptions, and generates a **simulation plan** — then, where
you have measured data, it validates and corrects those predictions and tells you honestly how far
you are from a scientifically valid comparison. The app uses nine primary pages and concise default
surfaces, so you first see the current state, the main blocker, and the next action. Open the clearly
named scientific detail controls when you need assumptions, full warnings, provenance, validation,
or verification guidance; technical identifiers and raw data stay collapsed by default. Critical
cautions remain visible. A selected machine uses four primary areas: **Overview**, **Prepare**,
**Results**, and **History**. You don't need to be a programmer to use it.

## 1. Install and launch

From the project folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Your browser opens the app. (Optional features — an AI import helper, the surrogate, the
assistant — need extra packages or an API key and stay hidden if they're not set up. The
app works fully without them.)

## 2. Create a durable project and a legacy experiment run

Create a durable project on **Projects** for the Phase 2 material workspace. The same page
also preserves the Phase 1 per-run workflow: open **Create new legacy run**, give the run a
name, and pick a **run type**:

- **lab_experiment** — your measured ICP / pH data (used to validate and correct predictions).
- **literature_benchmark** — values reported by other papers, kept *separate* from your
  lab data.
- **synthetic_demo** — fake data for testing the app only, never scientific output.
- **plastic_composite** — a lab-like side project.

Use the **Legacy experiment run** selector on **Projects** to change the active Phase 1 run.
The Material Workspace and Validation & Uncertainty pages use that exact selected legacy run
when you enter the embedded Phase 1 workflows.

## 3. Your first import (Validation & Uncertainty)

Open **Validation & Uncertainty**, choose its **Import** workflow, and upload a `.csv` / `.xlsx` /
`.xls` file, or type rows in by hand. The importer:

1. suggests how your columns map onto the app's fields (you confirm or fix the mapping);
2. converts chemistry columns to **mM** if you tell it the original unit (mg/L, ppm, ppb)
   — and keeps a record of every conversion so it can be checked later;
3. shows a preview and a validation summary **before** anything is saved;
4. saves to the run only when you confirm.

See **Input formats** in this guide for the exact column and unit rules. Nothing is saved
until you tick the confirmation box.

## 4. Then what?

The active workflow shows a **➡️ Next step** hint for your run. The embedded Phase 1 workflows remain
available under the nine-page shell; these workflow names are separate from the four areas in a
selected machine workspace. In short:

- **Simulate** — describe an experiment in plain language and get a structured scenario plus a
  simulation plan/matrix (the forward-looking core; no deterministic model is run yet).
- **Validate** — look at your measured data on its own and check the calculations.
- **Match** — link each measured record to the model result for the same conditions.
- **Compare Results** — run the workflow and read the comparison (counts, residuals, validity).
- **Export** — build a self-contained report you can hand to an advisor or committee.

Read **Mapping guide** and **Interpreting results** next — they explain what the statuses
and numbers mean, and what the app deliberately does **not** claim.
