# ML Surrogate Prediction Engine

The **ML surrogate engine** (`flyash_phreeqc_ml.ml_models/`) is the app's first *trained-model*
prediction engine for domains that have **no validated simulation engine** — starting with
**polymer-composite / fly-ash + plastic mechanical properties** (compressive strength first). It
turns the Evidence Library's curated, cited data into a fast screening estimate.

It is reached through the **Prediction Models** section, and the assistant *offers* it when a model
exists — but it is deliberately separate from the geochemical result path and from the AI layer.

## What it is — and is not

| It is | It is **not** |
| --- | --- |
| a data-trained **surrogate** (random forest / gradient boosting / ridge) | a validated design or safety value |
| a **screening** estimate with an uncertainty range + applicability warnings | a measurement |
| trained on **approved** evidence / lab rows (with provenance + confidence) | trained on unreviewed AI-extracted rows by default |
| labelled at most **experimental** (cross-validated) | ever labelled "validated" (that needs measured experiments) |
| a separate **prediction engine** | the geochemical (PHREEQC) engine, and not on its result path |

Key honesty statements (also surfaced throughout the UI):

* **The AI does not simulate or guess the number.** A scikit-learn model produces the prediction;
  the language model only *understands the request and routes it*. PHREEQC is the leaching engine,
  **not** the strength engine.
* **A model must be trained on approved data.** AI-extracted literature rows arrive `pending` and
  must be reviewed/approved before they can train a real model. Low-confidence or provenance-less
  rows are excluded by default. Synthetic **demo** rows are quarantined from real datasets.
* **Predictions carry uncertainty + applicability warnings** (out-of-domain inputs, unseen
  categories, blank-and-imputed features, implausible outputs).
* **Demo models are not validated** — they train on clearly-labelled synthetic data for workflow
  testing only, and their numbers are meaningless.
* **Real validation still requires measured experimental data** — compare predictions with
  measured results in the Validate / Compare workflow. A cross-validation R²/MAE describes the
  training distribution, not field performance.

## Targets + features

**Targets** (`model_schema.SUPPORTED_TARGETS`): `compressive_strength_MPa` (main),
`flexural_strength_MPa`, `density_g_cm3`, `water_absorption_percent`.

**Features** (`feature_schema`): material/binder oxides (`SiO2_wt`, `Al2O3_wt`, `CaO_wt`, …), mix
proportions (`red_mud_percent`, `cement_percent`, `aggregate_percent`), plastic
(`plastic_type`/`form`/`particle_size_mm`/`dosage_percent`/`replacement_basis`), and mixing/curing
(`water_binder_ratio`, `activator_type`/`concentration_M`, `curing_time_days`/`temperature_C`/
`condition`, `specimen_geometry`, `test_standard`). At least one **core** feature must be supplied
to predict (otherwise the engine refuses rather than guess).

## Modules

| Module | Role |
| --- | --- |
| `model_schema.py` | targets, model types, validation statuses, and the `TrainedModel` container (no scikit-learn) |
| `feature_schema.py` | numeric + categorical feature lists, labels, and the core-feature set |
| `training_data.py` | `TrainingRow` (+ provenance + `user_review_status`), evidence→row mapping, **eligibility**, demo rows, JSONL persistence |
| `preprocessing.py` | rows → model frame; the `ColumnTransformer` (median-impute numerics, one-hot categoricals tolerant of unseen values) |
| `train.py` | the **gate**, out-of-sample metrics (held-out / k-fold CV), final fit; typed errors; graceful no-sklearn |
| `uncertainty.py` | an approximate interval (random-forest spread, else CV residual σ) |
| `model_card.py` | the honest, exportable model card (intended/NOT-intended use, limitations, applicability, …) |
| `predict.py` | a prediction with value + interval + warnings + status; refuses on no-model / unsupported / incomplete |
| `model_registry.py` | save / load / list under a **safe**, gitignored folder; overwrite guard; cheap availability queries |

## Training-data eligibility (the gate that keeps it honest)

`training_data.eligible_rows(rows, target=…)` returns `(kept, excluded_with_reasons)`. By default a
row is eligible only if it: has a numeric value for the target; is `user_review_status == approved`;
has provenance (a DOI / citation / source, or is a lab/manual row); and — for **literature** rows —
clears the confidence threshold (`DEFAULT_MIN_CONFIDENCE = 0.45`). **Synthetic demo** rows are
excluded unless explicitly included. The UI exposes an *exploratory mode* that opts the excluded
rows in, clearly flagged as exploratory.

A **real** model is refused below `train.MIN_REAL_TRAINING_ROWS` (10) eligible rows with a typed
`InsufficientTrainingDataError` and the message *"Not enough approved data to train a reliable
model."* Demo training bypasses the gate (synthetic data only).

## Training behaviour

`train.train_model(rows, *, target, model_type, demo=False)`:

* validates the target, checks scikit-learn (raising a clear `SklearnNotAvailableError` if absent),
  and applies the gate;
* builds the preprocessor + estimator pipeline (random forest by default);
* reports **out-of-sample** metrics — a held-out split (≥ 24 rows) or k-fold cross-validation
  (smaller sets): MAE, RMSE, R² (only when ≥ 5 validation points), `n_train`/`n_validation`,
  feature coverage, missingness;
* fits the final model on all eligible rows and builds a model card;
* labels the model `experimental` (or `demo`) — **never** `validated`.

## Prediction behaviour

`predict.predict(model, features)` returns a `Prediction` carrying: the value; an approximate 95%
interval + σ + method; model name/version, training-row count, model source; the status
(`demo`/`experimental`); and warnings (not-validated, demo, missing-feature, out-of-domain,
unseen-category, implausible-output). It **refuses** (an explained, non-raising result) when there
is no model, the target is unsupported, or no core feature is supplied.

## Model registry + model card

Models are saved per run under `experiments/<run>/outputs/model_registry/<name>/`
(`run_manager.model_registry_dir`) — **gitignored**. A safe-path guard refuses to write into the
source tree, `data/raw`, or `data/processed`, and an existing model is never overwritten without
explicit confirmation. Each model folder holds the joblib artifact, the JSON model card, and a
small `meta.json` index (used for cheap availability queries). Model cards export as Markdown.

## Demo mode

`train.train_demo_model()` trains on `training_data.demo_rows()` — **synthetic** data encoding an
arbitrary made-up relationship, purely to exercise the workflow. The model and every prediction are
loudly marked **DEMO**, never described as validated, and never mixed with real evidence/lab rows
unless the user explicitly enables exploratory training.

## Assistant integration

When you ask for a prediction in a mechanical/composite domain ("predict the compressive strength
of fly ash + PET plastic after 28 days"):

* the domain classifies as `polymer_composite` (planning-only) — PHREEQC is **never** offered as the
  strength engine;
* **if a trained model exists** for the run, the assistant *offers* an experimental (not validated)
  estimate via **Prediction Models** (it never runs the model or produces a number itself);
* **if no model exists**, it offers to search literature / build a dataset / create a data template
  — it never fabricates a prediction.

The "is a model available?" check is a read-only registry query done in the UI and passed to the
agent as a flag; the agent imports nothing from `ml_models`.

## Boundaries (tests)

`tests/test_ml_models.py` pins the contract (schema/provenance, eligibility, the gate, training
metrics + card, registry safe-paths + overwrite guard, prediction value/interval/warnings/refusals,
demo labelling, and the assistant routing). `tests/test_ai_boundary.py` pins the imports: no
`ml_models` module imports an AI client or references a raw LLM response / API key; none imports an
executor; the geochemical result path and the simulation layers never import `ml_models`.

## Limitations

This is a **v1 surrogate scaffold**. With little approved data, estimates are wide and uncertain;
metrics are cross-validation only; the feature set is fixed; and there is no measured-experiment
validation yet (so no model is ever "validated"). It is a screening and prioritisation aid, not a
design tool.
