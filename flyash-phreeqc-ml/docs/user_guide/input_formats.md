# Input formats

The **Import Data** tab accepts two kinds of file input: **measured data** (what you measured)
and **model predictions** (what a model predicts) — separate from the plain-language experiment
description used by the **Simulate** tab. Each has a documented contract so the app never has to
guess.

## Measured data (Import Data tab)

- **Files:** `.csv`, `.xlsx`, `.xls`. For Excel you pick one sheet; the sheet has one
  header row.
- **Columns:** the importer suggests a mapping from your headers onto the app's fields
  (sample id, date, leachant, conditions, pH, and the chemistry columns). You confirm or
  correct it — different column names and orders are fine.
- **Units:** chemistry columns are stored in **mM**. If your file reports a different unit
  (mg/L, ppm, ppb), tell the importer and it converts using standard molar masses, keeping
  the original value, original unit, and the conversion it used. An **unrecognized unit is
  refused, not guessed** (e.g. it will say `unit 'g/L' not recognized for Ca; supported:
  mg/L, ppm, ppb, mM`).
- **Unknown columns are kept**, not dropped (prefixed `extra__`).

The full, exact rules live in [`docs/input_format.md`](../input_format.md) — the supported
units table, the conversion-provenance columns, and what happens to unknown columns.

## Model predictions (Import Data tab → "Import model predictions (CSV)")

The app is **not tied to one model**. Any model can supply predictions through a simple
CSV contract:

- required columns `record_key` (a unique id per prediction) and `model_name`;
- prediction columns named for each variable: `pred_pH`, `pred_Ca_mM`, … in the app's
  target units;
- optional metadata columns (leachant, concentration, time, L/S, condition code) so the
  app can suggest which measured records each prediction matches.

These predictions then flow through the **same** matching and comparison as the built-in
model. The full contract — required columns, unit handling, and the specific errors the
parser raises — is in
[`docs/model_prediction_format.md`](../model_prediction_format.md).

## What "supported" means

The shapes the app supports are pinned by a test matrix (`tests/matrix/`): fly-ash +
the built-in model, a literature benchmark kept separate, exact hand-checked residuals, a
differently-formatted upload resolved through the unit contract, an alternate dataset
profile, and a non-built-in model via the prediction CSV. The README lists these claims —
the app supports what those tests prove.
