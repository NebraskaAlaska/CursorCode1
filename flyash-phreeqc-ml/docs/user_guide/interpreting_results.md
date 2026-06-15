# Interpreting results

This page explains how to read the **Compare Results** tab: the residual sign, the counts panel,
the scenario-level collapse warning, and — importantly — **what this app does not prove**.

## Residual sign convention

For every variable:

```
residual = measured − model predicted
```

- **Positive** residual → the measurement is **higher** than the model predicts.
- **Negative** residual → the measurement is **lower** than the model predicts.
- **Near zero** → close agreement — **but only meaningful if the mapping is exact** (see
  the Mapping guide). A near-zero residual on a scenario-level or unsafe mapping does not
  mean the model is right.

## The counts panel (inclusion)

The comparison is explicit about *what is plotted and why the rest is excluded*. For the
selected variable you see:

- **Measured rows** — how many measured values exist.
- **With mapping** — how many are linked to a model result.
- **Prediction available** — how many of those have a model value for this variable.
- **Rows plotted** — how many actually appear in the comparison.
- **Excluded rows** — every excluded row, each with **exactly one reason** (no saved
  mapping / mapping is unsafe / model prediction missing / measured value missing).

Plotted + excluded always add up to the total — nothing is hidden.

## Scenario-level collapse

If many measured rows map to **just a few** model predictions, the scatter "collapses"
toward a vertical line: the measurements vary but the model value barely does. The app
flags this with a 🔁 warning. It usually means the mapping is too coarse, or you need more
model results for the individual conditions — not that the model is precisely right.

## The validity line

The Compare Results tab ends with one **validity** status for the comparison:

- **valid** — the *only* status that means the model was validated for that variable
  (all plotted mappings are exact and there are enough rows);
- **preliminary** — scenario-level mappings are included, so it's a workflow check;
- **single-sample** — only one mapped condition is plotted (not a trend);
- **unsafe** — known metadata conflicts are shown (for inspection only);
- **needs new simulations** — nothing is plottable yet for this variable;
- **nothing to compare** — no measured/predicted pairs exist.

## What this app does **not** prove

- **Plots are not validation.** A graph that looks like agreement is not evidence unless
  the validity line says **valid**. Whenever it doesn't, the report and the tab say so in a
  standing banner: *"This comparison is {status} — it is a workflow check, not model
  validation."*
- **A small residual is not proof.** It can occur for the wrong reasons — a coarse
  mapping, compensating errors, or a single tuned sample.
- **pH-only data only validates pH.** Ca/Si/Al/Fe/REE conclusions need ICP chemistry.
- **The model is a prediction, not a measurement.** Model outputs are thermodynamic
  predictions; the app parses them, it does not treat them as ground truth.
- **The descriptive bias bands and the experimental residual correction are not a
  validated correction model** — they describe this dataset's exact-mapped comparisons and
  are shown as raw-vs-corrected overlays only.

When in doubt, read the **validity line** and the **mapping status** together — they are
the honest summary of where you stand.
