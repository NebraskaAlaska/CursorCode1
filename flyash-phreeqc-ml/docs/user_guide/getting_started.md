# Getting started

This app is an **AI-assisted platform for geochemical / material-leaching simulation and
validation**. You describe an experiment and the variables you want; it extracts a structured
scenario, flags missing info and assumptions, and generates a **simulation plan** — then, where
you have measured data, it validates and corrects those predictions and tells you honestly how far
you are from a scientifically valid comparison. It is organized as tabs — **Start** (overview),
**Simulate** (describe an experiment → scenario → simulation plan), **Import Data**, **Validate**,
**Match**, **Compare Results**, and **Export**. You don't need to be a programmer to use it.

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

## 2. Create a run

A **run** is a save file for one experiment set. In the left **Experiment runs** sidebar,
open the *Create run* panel, give it a name, and pick a **run type**:

- **lab_experiment** — your measured ICP / pH data (used to validate and correct predictions).
- **literature_benchmark** — values reported by other papers, kept *separate* from your
  lab data.
- **synthetic_demo** — fake data for testing the app only, never scientific output.
- **plastic_composite** — a lab-like side project.

The sidebar always shows which run is active; every tab works on that run.

## 3. Your first import (Import Data tab)

Open the **Import Data** tab and upload a `.csv` / `.xlsx` / `.xls` file, or type rows in by
hand. The importer:

1. suggests how your columns map onto the app's fields (you confirm or fix the mapping);
2. converts chemistry columns to **mM** if you tell it the original unit (mg/L, ppm, ppb)
   — and keeps a record of every conversion so it can be checked later;
3. shows a preview and a validation summary **before** anything is saved;
4. saves to the run only when you confirm.

See **Input formats** in this guide for the exact column and unit rules. Nothing is saved
until you tick the confirmation box.

## 4. Then what?

The top of every tab shows a **➡️ Next step** hint for your run. In short:

- **Simulate** — describe an experiment in plain language and get a structured scenario plus a
  simulation plan/matrix (the forward-looking core; no deterministic model is run yet).
- **Validate** — look at your measured data on its own and check the calculations.
- **Match** — link each measured record to the model result for the same conditions.
- **Compare Results** — run the workflow and read the comparison (counts, residuals, validity).
- **Export** — build a self-contained report you can hand to an advisor or committee.

Read **Mapping guide** and **Interpreting results** next — they explain what the statuses
and numbers mean, and what the app deliberately does **not** claim.
