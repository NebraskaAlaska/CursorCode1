# Model-prediction CSV contract (non-PHREEQC models)

The comparison workflow is **model-agnostic**: any model — not just PHREEQC — can
supply predictions through this documented CSV contract. The parser
(`flyash_phreeqc_ml/parsers/generic_prediction_parser.py`) validates a file against the
contract and normalizes it; `scenarios.build_scenario_manifest` then consumes it exactly
like a parsed PHREEQC result, so the suggestion engine, mapping statuses, inclusion
logic, residuals, and plots **never know which model produced the numbers**.

This is registered as `profiles.GENERIC_CSV_PROFILE` (a `ModelProfile` whose
`parser_entry_point` is this parser); `profiles.PHREEQC_PROFILE` keeps the `.pqo` path.

## Required columns

| Column | Meaning |
| --- | --- |
| `record_key` | unique join key for each prediction row (non-blank, no duplicates) |
| `model_name` | the model that produced the row (shown in the UI) |

Plus **at least one prediction column** (below).

## Prediction columns (named per the dataset profile's variables)

For each comparison variable the profile declares, the prediction column is:

| Measured variable | Prediction column | Target unit |
| --- | --- | --- |
| `final_pH` | `pred_pH`   | pH |
| `Ca_mM`    | `pred_Ca_mM` | mM |
| `Si_mM`    | `pred_Si_mM` | mM |
| `Al_mM`    | `pred_Al_mM` | mM |
| `Fe_mM`    | `pred_Fe_mM` | mM |

Values must be in the profile's **target units** (mM for `*_mM`, pH for pH). If a file
reports a different unit, declare it via a `units` mapping (`{pred_column: unit}`) — a
header row or a sidecar — and the parser converts it through the **Prompt-16 registry**
(`flyash_phreeqc_ml/units.py`), tagging the conversion id as provenance
(`predicted_<X>__conversion_id`). With no declaration the target unit is assumed and the
id is `identity`.

## Optional metadata columns (must match the profile's mapping fields)

Passed through when present, so the rule-based suggestion engine can score the mapping:
`leachant`, `NaOH_M`, `acid_M`, `time_min`, `liquid_solid_ratio`, `CO2_condition`,
`condition_code`, `temperature_C`, and an optional `scenario_label`.

## Example

```csv
record_key,model_name,pred_pH,pred_Ca_mM,leachant,liquid_solid_ratio,CO2_condition
M1,ToyThermo,12.8,1.9,NaOH,5,OA
M2,ToyThermo,12.6,1.4,NaOH,5,PF
```

## Validation errors (specific, never silent)

| Error | When |
| --- | --- |
| `MissingRequiredColumn` | `record_key` or `model_name` absent |
| `NoPredictionColumns` | no `pred_*` column for any profile variable |
| `BlankRecordKey` | a row has a blank `record_key` |
| `DuplicateRecordKey` | `record_key` repeats |
| `InvalidPredictionValue` | a prediction cell is present but not numeric |

Unknown units raise `units.UnknownUnitError` (the Prompt-16 contract). There is **no
silent fallback** anywhere.

## How it reaches the app

The Data tab's **"Import model predictions (CSV)"** expander (lab-like runs) uploads,
validates, previews the resulting manifest, and on confirm writes
`data/processed/model_predictions.csv`. When that file is present it is the manifest
source (precedence over `phreeqc_results.csv`), so the Match tab and Results compare
against the generic model.

## Known PHREEQC-isms in the manifest (kept for compatibility, flagged)

The manifest abstraction is model-agnostic in behaviour but two **column names** still
carry the historical `phreeqc_` prefix. They do **not** block a non-PHREEQC model — they
are populated from whatever model produced the predictions — and renaming them touches
every downstream consumer, so they are left as-is and flagged here for a future,
deliberate rename:

- `phreeqc_record_key` — the manifest's join-key column (holds the generic `record_key`).
- `phreeqc_pH` / `phreeqc_<X>_mM` — the comparison's model-prediction columns
  (`compare.predictions_mM_from_manifest` maps `predicted_*` → these).

Nothing else in the manifest, suggestion, mapping-status, inclusion, residual, or plot
code is PHREEQC-specific.
