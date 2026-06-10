# Comparison inclusion rules

The measured-vs-model comparison display is **explicit about inclusion**: every
measured row is either *plotted* or *excluded with one stated reason*, and one
overall *validity* line summarises the result. All of this is decided in a single
pure function, `flyash_phreeqc_ml/compare/inclusion.py :: comparison_inclusion(...)`.
The plots and the counts panel consume its output — they never re-derive filters.

## Per-row outcome (one per row, priority order)

For the selected `variable` each comparison row gets the **first** applicable label:

1. **`no saved mapping`** — the row has no `phreeqc_record_key`.
2. **`mapping is unsafe (excluded by default)`** — the mapping status is `unsafe`
   (e.g. an acid leachant mapped to a NaOH/CO₂ scenario, or opposite CO₂ families)
   and the *Include unsafe* toggle is off.
3. **`model prediction missing this variable`** — mapped, but the model has no value
   for this variable (e.g. PHREEQC does not predict Fe).
4. **`measured value missing/non-numeric`** — mapped and predicted, but the measured
   value is blank or non-numeric.

Otherwise the row is **plotted**. Plotted ∪ excluded partitions the comparison rows,
so `rows_plotted + len(excluded) == n_total` always holds (the counts panel and the
exclusion table are therefore mutually consistent by construction).

A row is plotted only when the mapping status is `exact` or `scenario-level only` —
**or** `unsafe` when the *Include unsafe* toggle is on, in which case those points are
**flagged** (`flagged=True`) and drawn in red.

## Scenario-level collapse warning

Triggers when plotted rows reuse too few model predictions:

```
collapse = rows_plotted >= 2 AND
           ( unique_predictions_used / rows_plotted <= 0.5
             OR  any single prediction reused >= 3 times )
```

Message: *"Many measured rows map to few model predictions — this comparison is
scenario-level; new simulations are likely needed for per-condition validation."*

## Overall validity (one line, first match wins)

| status | rule | implies model validated? |
|---|---|---|
| `nothing to compare` | nothing plotted **and** no measured values exist | no |
| `needs new simulations` | nothing plotted but measured values exist (need mappings/sims) | no |
| `unsafe` | *Include unsafe* on **and** an unsafe row is plotted | no |
| `single-sample` | exactly one row plotted | no |
| `valid` | every plotted mapping is `exact` **and** rows_plotted ≥ `min_valid_rows` (default 3) | **yes** |
| `preliminary` | anything else (scenario-level included, or too few exact rows) | no |

`valid` is the **only** status that asserts the model was validated. No other status
ever produces text implying PHREEQC is validated. The previous standalone
single-sample warning is folded into the `single-sample` status here.

## Residual sign convention

`residual = measured − PHREEQC`; positive means the measured value is higher than the
model prediction. Near-zero residuals indicate agreement **only if the mapping is
scientifically valid**.
