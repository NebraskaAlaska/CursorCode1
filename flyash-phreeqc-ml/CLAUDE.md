# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

WPI **Class C fly ash + PHREEQC** geochemical modelling project. It combines PHREEQC
speciation/equilibrium simulations of high-pH (~13) Na–Si–Al–Ca alkali-activated systems
(CEMDATA18 database, CO₂ carbonation) with experimental ICP measurements, working toward
predicting measured fly-ash outcomes (Ca/Si/Al/Fe/REE/Sc release, pH, carbonate formation).

The long-term aim is an ML *correction* layer that learns where PHREEQC disagrees with
experiment — **not** a blind replacement for the chemistry.

### Completed phases

- **Phase 1 — parse + analyze.** Parsers for `.pqi`, `.pqo`, `SELECTED_OUTPUT`, and the ICP
  workbook produce clean processed CSVs and `master_dataset.csv`, plus exploratory plots.
- **Phase 2 — PHREEQC vs experiment.** A measured-experimental-release template + parser, and
  a residual comparison (`measured − PHREEQC` for Ca/Si/Al/Fe/pH) with measured-vs-PHREEQC plots.
  The machinery is in place and tested but **dormant until measured data exists**.
- **Experiment-planning + QA/QC tooling** (pre-data, no ML). `flyash_phreeqc_ml/experiments/`
  (the *package*) + scripts 06–08: generate the run sheet, validate a filled release CSV
  (error/warning report), and compute sustainability *proxy* indicators. These run before
  measured data exists and feed Phase 2.
- **Experiment Run Manager** (app-level "save files", no ML). `flyash_phreeqc_ml/run_manager.py`
  + the top-level `experiments/` *data folder*. Lets one app hold several independent runs
  (lab / literature / synthetic / plastic-composite), each in `experiments/<safe_name>/` with a
  `run_config.yaml`, `data/`, and `outputs/`. It is a save/open layer over the existing workflow,
  not a replacement, and it keeps literature data out of the measured-release file.
- **Calculation verification / formula audit** (transparency, no ML). `flyash_phreeqc_ml/calculations.py`
  + the app's **Calculation Verification** view (in the Audit / Help tab) document every downstream formula (residuals, ICP
  mg/L→mM, dilution, L/S ratio, mass released, recovery) and **re-derive** the stored residuals to
  confirm they match (`pass`/`warning`/`fail`/`not available`). PHREEQC's SI and pH are explained,
  not recomputed.
- **Mapping assistant + scenario explorer** (guided mapping, no ML). `flyash_phreeqc_ml/scenarios.py`
  builds a readable **PHREEQC scenario manifest** from `phreeqc_results.csv` (molality→mM, metadata
  inferred from the source filename where safe, else `unknown`) and scores each scenario against a
  measured sample with **transparent rule-based** weights (no learning) so the Match PHREEQC tab can
  *suggest* the best PHREEQC rows, flag samples needing a new simulation, and warn on collisions.
  Plus **lab CSV upload** for lab runs, **mapping-quality** checks, and the app no longer generates
  experiment plans — its job is now ingest → verify → map → run → interpret.
- **Guided-workflow UI** (UI-only reorg, no chemistry/ML). The Streamlit app was condensed from ten
  scattered tabs into **five workflow tabs** — **Start**, **Data**, **Match PHREEQC**, **Run +
  Results**, **Audit / Help** — matching the ingest → verify → map → run → interpret order. No
  functionality was removed: it was relocated (e.g. Run Workflow + Results merged; PHREEQC Outputs +
  Calculation Verification + Help folded into Audit / Help; the old Tools/Literature tabs absorbed
  into Data / Run + Results). Start adds a data-quality line, a richer recommended-next-action, and a
  workflow checklist.
- **Flexible + dissolution-workbook import** (ingest, no chemistry/ML). The Data tab's lab uploader
  is now two modes (`_lab_data_import`): a **generic** `.csv`/`.xlsx`/`.xls` importer
  (`import_mapping.py` — sheet pick, fuzzy column mapping, mg/L·ppm·ppb→mM, leachant/provenance,
  pre-save validation, confirm-gated replace/append) and a special-case **Class C fly ash
  dissolution-workbook** parser (`dissolution_workbook.py` — horizontal ICP OES element blocks with
  mmol/l preferred, pH read from the pH column, HCl kept acid-tagged), validated against the real
  file. Both keep unknown columns and write through `run_manager.save_lab_dataframe`.
- **Replicate-aware mapping + presentation status** (no chemistry/ML). `replicates.py` treats PHREEQC
  `sol1/sol2/sol3` as replicate/batch outputs of one condition, not time points: `condition_key`
  grouping, replicate ids, mean±std summaries, **condition-level mapping** (replicates inherit one
  link, expanded to the per-sample map; storage in `run_manager`), an optional replicate→solution
  path, replicate-aware collision rules, and condition mean / individual comparison modes. Layered on
  top, a **mapping-status** classifier (`exact` / `scenario-level only` / `unsafe` / `needs new
  simulation`) drives the Start **Presentation summary**, the Run + Results "workflow check,
  not final validation" warning, and the "conditions needing new PHREEQC simulations" table — framing
  the comparison as a **preliminary validation workflow**, not an overclaimed model.

- **OA/PF/GS cover conditions known** (metadata semantics, no chemistry/ML). OA/PF/GS are now
  documented cup-cover / CO₂-exposure conditions — **OA = open air** (atmospheric CO₂),
  **PF = plastic flap cover**, **GS = glass cover** (PF/GS covered, reduced exchange, *not* sealed
  unless airtight-confirmed). `scenarios.cover_condition` / `co2_exposure_level` map a code to
  `open_air`/`plastic_flap`/`glass_cover` and `open`/`reduced`/`reduced`; the dissolution importer
  emits derived `extra__cover_condition` / `extra__CO2_exposure_level` columns and **warns** when a
  shared `CO2_condition=open` default lands on covered PF/GS rows (so "open" default ≠ "PF/GS are
  open-air"). Mapping still treats OA/PF/GS as distinct conditions (`condition_key`). The wording is
  kept **project-specific**: the explanation only surfaces for datasets that actually contain those
  codes.
- **Generic Match-tab presentation** (UI wording, no chemistry/ML). The Match PHREEQC tab's core
  interface is worded **experiment-agnostically** — "Match measured data to model predictions",
  selectors **Measured data group** + **Model / simulation result**, generic mapping-status
  definitions (`MAPPING_STATUS_DEFINITIONS` in `replicates.py`) — so the app reads as a generic
  *measured data → model prediction → mapping → residuals → validation status* workflow. PHREEQC and
  the fly ash OA/PF/GS / CO₂-cover metadata are kept as the **current project implementation**:
  PHREEQC file/source/solution number live in the **Advanced validation metadata** expander (now
  rendered **dynamically** from whatever metadata columns the dataset actually has), and the OA/PF/GS
  explanation only appears when the loaded run's rows actually carry those codes
  (`_dataset_condition_codes`). A design note in Audit / Help states PHREEQC + fly ash metadata are
  the implementation, not a hard limit. No backend metadata, status logic, or warnings were removed.

- **Automatic-first Match tab** (UI flow, no chemistry/ML). The Match tab is now
  **auto-detect → auto-suggest → review → accept → graph** rather than dropdown-first.
  `_extract_measured_records` auto-detects measured records (sample_id, measured group / condition
  key, time, populated measured variables, units, notes — only from columns the dataset has);
  `_build_auto_suggestions` produces one suggestion per measured data group via the **transparent
  rule-based** `scenarios.suggest_mappings` (no opaque ML), each row carrying measured_record /
  model_prediction_record / status / confidence / matched / missing / conflicting fields and a
  "why was this suggested" explanation. The suggestion table is an editable `st.data_editor` with an
  **accept** checkbox; buttons **Accept all high-confidence** / **Accept selected** / **Clear
  suggested** / **Export mapping** (`_accept_condition_mappings` upserts via `add_condition_mapping`
  then `apply_condition_mapping`). When a run has no measured rows the tab shows "No measured data
  found for this run…" and clears stale per-run suggestion state instead of showing dropdowns. The
  former dropdown mapping (condition + per-sample) is preserved under a **Manual override / advanced
  mapping** expander. Graphs still need only measured + model-predicted values + a saved mapping.

- **Consolidated suggestion-table Match tab** (UI/consolidation, no chemistry/ML, no scoring
  change). The Match tab is driven by **one** auto-generated suggestion table (new
  `mapping_table.build_suggestion_table` — one row per `condition_key`: best candidate scenario,
  `mapping_status`, score, confidence, reason, `already_mapped`). It renders at the top with no
  button as soon as run data + `phreeqc_results.csv` exist; the status column is badged. Row detail
  (a per-condition selector) shows the field-by-field measured-vs-model alignment, runner-up
  candidates, and a structured **score breakdown** (`scenarios.score_scenario` now also returns
  `score_breakdown` — additive, the point values are unchanged). Accept actions: **Accept all
  exact** (bulk, exact-only), per-row **accept** checkboxes + **Accept selected** (exact +
  scenario-level, with a caution); **unsafe** rows cannot be accepted from the table — they route to
  the **Manual override / advanced mapping** expander, which requires a confirmation checkbox and
  tags the saved mapping `override=true` (new `override` column in the condition map; never reaches
  the per-sample map). The **conditions-needing-simulation** section is driven from the *same* table
  so counts always agree. The scenario explorer and per-sample assistant are demoted to
  **Explore PHREEQC scenarios** / **Per-sample assistant (advanced)** expanders (all functionality
  kept). Covered by `tests/test_suggestion_table.py`.

- **Explicit comparison inclusion** (presentation honesty, no chemistry/ML). The Results-tab model
  comparison is now explicit about *what is plotted and why the rest is excluded*. One pure function
  `compare/inclusion.py :: comparison_inclusion` is the **only** inclusion logic (the plots consume
  its output): per selected variable it partitions rows into plotted vs excluded-with-one-reason,
  joins mapping statuses, plots `exact`/`scenario-level` by default (unsafe excluded unless an
  advanced toggle flags them red), warns on scenario-level **collapse** (many rows → few model
  predictions), and ends with one **validity** line (`valid` only when all plotted mappings are exact
  and ≥ `min_valid_rows` — the single case that implies validation). The app's Run + Results tab shows
  a counts `st.metric` panel, an "Rows excluded from model comparison" expander, a status-styled
  measured-vs-model scatter (`viz/compare_plots.comparison_scatter_figure`), the collapse warning, and
  the validity line; residual-figure captions carry the `measured − PHREEQC` sign convention + "near-
  zero residuals indicate agreement only if the mapping is scientifically valid." Rules in
  `docs/comparison_inclusion.md`. `scripts/05` CLI behavior is unchanged.

- **CO₂ cup-cover vocabulary (corrected)** (scientific matching change). `CO2_condition` now uses the
  cup-cover vocabulary — **OA/PF/GS** (experiment) + **atm_CO2/low_CO2/no_CO2** (model) + `unknown`
  (`config.CO2_CONDITION_ALLOWED`, with `config.CONDITION_CODE_DESCRIPTIONS` as the single source for
  descriptions + the not-confirmed-sealed caution the UI reads). `scenarios.co2_family` now returns
  **atmospheric** / **reduced** families (never "sealed"); the cover cap in `_metadata_alignment` was
  restricted to PF/GS so **OA can reach `exact`** against an atmospheric scenario while **PF/GS cap at
  `scenario-level only`** until a model scenario explicitly represents the cover (`replicates.mapping_status`).
  The dissolution importer writes the cover code into `CO2_condition` per row; the generic importer
  maps legacy `open`→`OA` and **flags** legacy `sealed`; `replicates.condition_key` no longer
  duplicates the cover; the plan generator's cover-control set is `{OA,PF,GS}`. See the CO₂-condition
  key-convention below.

- **Dataset/model profile layer** (generalization, additive, no chemistry/ML). `profiles.py`
  introduces `DatasetProfile` + `ModelProfile` (fly-ash / PHREEQC instances referencing `config.py`)
  threaded through the existing seams with a fly-ash default — so the same condition-grouping →
  suggestion-table → mapping-status → inclusion → measured-overview chain runs for another dataset by
  passing a different profile, with **zero behaviour change** for fly ash. app.py UI strings take the
  model name from `ModelProfile`, and a generic-wording pass moved non-mechanics captions to
  "model"/"measured data" (PHREEQC stays in the scenario explorer, per-sample assistant, advanced
  metadata, the Audit/Help PHREEQC-outputs viewer, parser/script messages, and the Match **PHREEQC**
  tab name). See the **`profiles.py`** architecture bullet. Package not renamed; `config.py` public
  constants unchanged.

- **High-end research-dashboard UI** (presentation only, no chemistry/ML). `app_ui.py` (new) is a
  pure styling layer — a global stylesheet plus helpers (`render_hero`, `render_page_header`,
  `section_header`, `status_badge`, `render_metric_cards`, `render_warning_panel`,
  `render_workflow_steps`, `advanced_expander`) with one shared status-colour system (exact=green,
  scenario-level=amber, unsafe=red, needs-new-sim=blue/purple, preliminary=amber; theme-agnostic
  translucent tints). `app.py` gained a hero, a per-tab page header + one-sentence purpose, workflow
  steppers, status cards, badge-styled mapping statuses, coverage cards, and amber "preliminary /
  workflow check only" panels — **all functionality, warnings, plots, and the scientific-honesty
  wording preserved** (only relocated/restyled). The global CSS is re-injected **every run** (Streamlit
  drops elements a rerun doesn't re-emit, so a once-only `<style>` vanishes on the first rerun).

- **Optional AI import-assist** (opt-in, suggestion-only; no AI in mapping or validation).
  `flyash_phreeqc_ml/ai/import_assist.py` (new) proposes interpretations of messy uploads in the Data
  tab's generic importer: `classify_sheets`, `propose_column_mapping`, and a **rule-first**
  `parse_sample_names` (the profile's sample-id conventions parse what they can; only the leftovers go
  to the LLM). Uses the Anthropic SDK (lazy import; `ANTHROPIC_API_KEY` → enabled, else hidden with a
  one-line caption — the app works fully without it). Strict-JSON responses are parsed defensively
  (fence-stripping, graceful fallback). A one-time per-session notice + consent checkbox gate any data
  leaving the machine. Suggestions land in the **existing review/confirm** flow (mapping editor +
  an editable metadata table, badged `ai-suggested`/`rule`); saved rows carry a `metadata_provenance`
  column (`rule`/`ai-confirmed`/`manual`). Nothing AI-touched is saved without the confirm-gated save.

- **On-demand PHREEQC runner** (Prompt 11 — plumbing, no ML). `flyash_phreeqc_ml/phreeqc_runner.py`
  (new) makes "needs new simulation" actionable: `build_input` templates a `.pqi` from a measured
  condition's metadata (OA → atmospheric `CO2(g)`; PF/GS → **both** a low-CO₂ and a no-CO₂ variant,
  since the cover's exchange rate is unconfirmed), with assumed stock chemistry written as **visible**
  comments; `run` executes the user-supplied PHREEQC **CLI** (`PHREEQC_EXE` + `PHREEQC_DATABASE` from
  the environment; CEMDATA18 is not redistributable) with a hard timeout and typed
  `PhreeqcNotConfiguredError`/`PhreeqcRunError`; `ingest` parses with the existing `pqo_parser`, appends
  to `phreeqc_results.csv` tagged `generated`/`source_condition_key`/`generated_at` (+ exact condition
  metadata), and regenerates the manifest. The Match tab's needs-new section gained a **Generate
  simulation** flow (preview the `.pqi` + assumptions panel → run → ingest → refresh); `scripts/09`
  batches it. Generated `.pqi`/`.pqo` live under `experiments/<run>/outputs/generated/` (gitignored).
  **Verified design behaviour:** a generated OA scenario reaches **exact** mapping; PF/GS reach
  **scenario-level only** (the Prompt-5 cup-cover cap). No real PHREEQC is installed here, so the run
  path is gated + unit-tested via mocks; one optional integration test runs only when configured.

- **PHREEQC surrogate (experimental)** (Prompt 12 — ML scaffolding, **not** in any result path).
  `flyash_phreeqc_ml/ml/sampling.py` (seeded Latin-hypercube design over `config.SURROGATE_INPUT_SPACE`)
  + `ml/surrogate.py` (one model per output: a Gaussian-process regressor — standardized inputs,
  Matérn + white-noise — with a HistGradientBoosting quantile fallback above
  `SURROGATE_GP_MAX_SAMPLES`; per-output **model card** with training-set hash, n, input ranges =
  validity domain, k-fold CV, library versions, date; `validate_surrogate` reporting held-out RMSE/MAE
  + 95%-interval coverage; `predict` flags inputs outside the trained box as `domain=extrapolation`).
  `scripts/10` writes the design then runs the batch through the runner into `surrogate_dataset.csv`,
  **recording non-converged runs with a `status` column** rather than dropping them. The Audit/Help
  tab gained a **Surrogate (experimental)** expander, always labelled "surrogate approximation of
  PHREEQC — not a measurement, not a PHREEQC run"; **surrogate values never enter comparison CSVs,
  residuals, or mapping.** Requires scikit-learn + scipy; trained models/datasets are gitignored run
  outputs.

- **Systematic-bias estimates (statistics, not learning)** (Prompt 13 — the *first* "residual
  correction", deliberately descriptive). `flyash_phreeqc_ml/ml/residual_stats.py` (new;
  **pandas/numpy only, no sklearn**) computes per-element / per-condition mean-residual bias over
  **exact**-mapped comparisons only. `bias_table(comparison_df, statuses, min_n=5)` returns one row
  per `(element, condition_key)` plus a pooled `(element, all-conditions)` row with `n_exact_pairs`,
  `mean_residual`, `std` (ddof=1), `sem`, a `sufficient` flag (`n ≥ min_n`), and `unit`; **below
  `min_n` the row is kept `sufficient=False` and the UI shows "insufficient exact pairs (k of N
  needed)", never a number.** Rows are filtered to status `exact` using the **inclusion module's
  status join** (`collect_sample_statuses` → `compare.inclusion`, the single source — scenario-level
  / unsafe / unmapped excluded), and **synthetic/demo rows are never counted even if marked exact**.
  Wording comes from the sign convention (`residual = measured − model`: positive mean → model
  **under**predicts) via `describe_bias_row`; `bias_direction` maps the sign, `sufficient_bias_bands`
  exposes the pooled mean±std for the shaded plot band, and `exact_residuals` is the plot-ready frame.
  Every rendering carries the explicit non-claim line (`NON_CLAIM_LINE`: "Bias estimates describe this
  dataset's exact-mapped comparisons; they are not a general correction model."). The Results tab
  gained a **"Systematic bias (exact mappings only)"** expander (table with the mean hidden below
  threshold + plain-language captions + a per-element residual scatter with a shaded mean±std band
  where sufficient). `scripts/05 --run` writes the table to `experiments/<run>/outputs/systematic_bias.csv`
  beside the comparison, so the **comparison provenance stamp** (which fingerprints the same three
  inputs) already covers its staleness. Covered by `tests/test_residual_stats.py`.

- **GP residual-correction model (experimental)** (Prompt 14 — the *learned* successor to the bias
  bands, hard-gated and display-only). `flyash_phreeqc_ml/ml/residual_model.py` (new; lazy/optional
  scikit-learn) fits one **GaussianProcessRegressor per element** whose **target is the residual**
  (`measured − model`) and whose **features are condition metadata** encoded via the dataset profile —
  leachant family, molarity, L/S, time (numeric, median-imputed) + the CO₂/cover code one-hot (new
  additive `DatasetProfile.feature_numeric_fields` / `feature_categorical_fields`). Three hard rules:
  (1) **Data-sufficiency gate** — `train_element_model` raises a typed `ResidualModelGateError`
  carrying the counts unless the element has **≥30 exact pairs across ≥3 distinct condition_keys**
  (`gate_status` / `ResidualGateStatus.progress_message` → "14 of 30 exact pairs; 2 of 3 conditions");
  the UI shows progress instead of a train button until met. Exactness reuses
  `residual_stats.exact_mask` (same Prompt-13 filter incl. synthetic exclusion — **this never weakens
  the Prompt-13 gate, only adds a stricter one**). (2) **Leave-one-condition-out validation**
  (`loco_cross_validate` / `_loco`) — not random k-fold, because generalizing to an *unseen condition*
  is the failure mode that matters; reports per-element LOCO RMSE **vs the Prompt-13 constant-bias
  baseline** (the held-out-fold training mean), with `beats_baseline` / `use_correction_recommended`;
  if it doesn't beat the constant bias the UI says so prominently (red) and recommends staying with
  the bias bands. (3) **Corrected = PHREEQC + predicted_residual with the GP interval**, shown only as
  an **off-by-default "Corrected (experimental)" overlay** (`compare_plots.corrected_overlay_figure`)
  that always draws **raw PHREEQC + correction + interval + measured together — never the corrected
  value alone**; corrected values **never feed mapping status, validity status, or the comparison
  CSV's residual columns**. A per-model **model card** (training run names + set hash, gate values,
  LOCO-vs-baseline, library versions, date) saves beside the model under
  `experiments/<run>/outputs/residual_model/` (gitignored; `run_manager.residual_model_dir`). The
  Results tab gained a **"Residual correction (experimental)"** expander (per-element gate progress →
  train button when met → LOCO verdict → optional overlay). Covered by `tests/test_residual_model.py`.

- **GP model-incompleteness estimator (experimental)** (Prompt 27 — model *where PHREEQC's mechanism is
  systematically short*; only after Prompts 22–25 exist + data accumulates). `flyash_phreeqc_ml/ml/incompleteness_model.py`
  (new) learns the **unexplained closure residual** (Prompt 24/25 — the gap PHREEQC could **not** attribute)
  per element as a function of batch conditions, **reusing `residual_model`'s machinery verbatim** (feature
  encoding, GP pipeline, `_loco`, training-hash/card helpers — imported, not reinvented). The output is
  framed strictly as a **"predicted systematic shortfall of the PHREEQC attribution under these conditions"**
  — never a measured amount, never fed into closure arithmetic (predictions live only in
  `predicted_shortfall*` columns). **Gate** (typed `IncompletenessGateError` with counts): ≥30
  **well-determined** rows across ≥3 conditions, where well-determined = closure-gap σ ≤
  `GAP_SIGMA_REL_TOL`·|gap| **and** a trustworthy (complete-closure) recovery status **and**
  `starting_provenance == "measured"` — so a row whose starting assay is a confirmed *or* proposed
  **literature** stand-in is **excluded from training** (the constraint: trains on measured closure gaps +
  modeled attributions only). **Noise guard** (`NoLearnablePatternError`): if the residual's reduced χ²
  around its mean ≤ `NOISE_CHI2_MAX`, training refuses with *"consistent with measurement noise; no
  learnable pattern"* rather than fitting noise. **LOCO** (leave-one-condition-out) vs the Prompt-13
  constant-bias baseline via `residual_model._loco`; `use_model_recommended` is True only when it beats the
  baseline (else stay with the bias bands). **Uses:** (a) `flag_underattributed_conditions` — the
  active-learning hook (conditions predicted to be strongly under-attributed → candidates for new
  experiments / better phase lists); (b) `incompleteness_overlay` — an off-by-default, clearly ml-predicted
  shortfall overlay for the recovery report. Per-element **model card** (gate values, signal assessment,
  LOCO-vs-baseline, training run names + set hash, library versions, date) + joblib persistence under
  `run_manager.incompleteness_model_dir` (gitignored). `build_recovery_dataset` assembles the per-row
  training frame from measured closure + modeled attribution + literature-provenance flags. Requires
  scikit-learn (via `residual_model`); tests use `optimizer=None` fast-mode. Covered by
  `tests/test_incompleteness_model.py` (gate 29-refused/30-accepted + ≥3-conditions, well-determined /
  literature / status filters, noise-domination refusal, LOCO + baseline logic, prediction-only columns,
  build-dataset target = measured gap − modeled attribution, persistence).

- **Grounded "Ask the assistant"** (Prompt 15 — interpretation layer, read-only, no invented numbers).
  `flyash_phreeqc_ml/ai/assistant.py` (new) answers natural-language questions about the **selected
  run** using the Anthropic **tool-use** API, where every tool wraps an existing read-only function so
  each number in an answer comes from the app's own code paths, never the model. Tools (all read-only,
  none mutates anything): `get_mapping_overview` (suggestion table + `overall_mapping_status`),
  `get_inclusion_counts` (Prompt-4 per-variable counts + the one validity status),
  `get_unsafe_mappings`, `get_needed_simulations`, `get_bias_table` (Prompt-13, carries the non-claim
  line), `get_comparison_rows` (capped), `get_run_provenance` (`comparison_is_current` + reasons), and
  `get_mapping_trace(condition_key)` (Prompt-6 scoring trace). A carefully-written `SYSTEM_PROMPT`
  (stored in the module) forces the model to **answer only from tool results, cite which tool each
  number came from, refuse to say "validated" unless `get_inclusion_counts` reports
  `validity=="valid"`, flag insufficiency and point to the gate counts, and frame "what should I test
  next" as suggestions** reasoned from the unsafe/needs-simulation/bias tools. `answer()` runs a finite
  tool_use→tool_result loop and returns an `AssistantAnswer(text, trace, ok, error)`; the `trace` lists
  every tool call + a short summary so the UI shows a collapsed **"data used"** panel under each
  answer. Disabled cleanly without `ANTHROPIC_API_KEY` (reuses `import_assist`'s lazy client). The UI
  is a chat box in **Audit / Help only** (kept out of the workflow tabs — an interpretation layer, not
  a step), scoped to the selected run, gated by the same per-session **data-leaves-machine consent**;
  conversation history is **session-only, never written to run files**. Covered by
  `tests/test_assistant.py` (tool shapes, dispatcher round-trip with a mocked client, disabled path).

- **Conversion provenance + single unit authority** (Prompt 16 — data-model honesty, no chemistry/ML).
  `flyash_phreeqc_ml/units.py` (new) is the **one place** any unit conversion happens: a
  `MOLAR_MASSES` registry (IUPAC standard atomic weights, cited in-code and surfaced in the UI) and a
  `CONVERSIONS` registry (each entry: `id`, `from_unit`, `to_unit`, human-readable `formula` string,
  function). `convert(value, from_unit, to_unit, element) -> ConversionResult` returns the converted
  value **plus** the registry id and parameters used (molar mass); identity (`from==to`) →
  `conversion_id="identity"`. **No silent fallbacks** — an unknown unit/element raises a typed
  `UnknownUnitError`/`UnknownElementError` (e.g. *"unit 'g/L' not recognized for Ca; supported: mg/L,
  ppm, ppb, mM"*). The old factors now route through it: `config.PHREEQC_MOLALITY_TO_MM =
  units.MOLALITY_TO_MM_FACTOR`, `calculations.ATOMIC_MASSES = units.MOLAR_MASSES`,
  `calculations.mgl_to_mM` and `import_mapping.convert_concentration`/`convert_series_to_mM` all call
  `units.convert`. **Provenance columns:** when the generic importer converts `X_mM` it keeps wide
  companions `X_mM_orig_value` / `X_mM_orig_unit` / `X_mM_conversion_id` (identity-tagged when already
  mM); they survive `run_manager.save_lab_dataframe` and are recognised (never `extra__`/unknown).
  **Re-derivation check:** `calculations.verify_conversions(df)` recomputes each converted column from
  its companions through the registry and reports pass/warning/fail per column (legacy runs without
  companions load and are flagged `unknown(legacy)`, never errored) — this catches a wrong molar mass
  or changed formula after the fact. **Defined input contract:** `import_mapping.validate_unit` refuses
  an undeclared unit using the profile's new additive `DatasetProfile.accepted_units`; documented in
  `docs/input_format.md`. **UI:** an "Unit conversions applied" expander (import preview + run-data
  viewer) shows original→target, formula, molar mass, and 3 example rows; the Calculation Verification
  view renders the molar-mass + conversion registries straight from `units.py` and a per-run
  conversion re-derivation table. Covered by `tests/test_units.py`.

- **Append-only audit log** (Prompt 17 — provenance of *actions*, no chemistry/ML). `flyash_phreeqc_ml/audit.py`
  (new) records how a run's comparison was produced: `log_event(run_name, event_type, payload)` appends
  **one JSON line** to `experiments/<run>/outputs/audit_log.jsonl` with `timestamp`, `event_type`,
  `app_version` (the package `__version__`), and a sanitised `payload`. **Append-only by construction** —
  the module exposes only `log_event` + typed convenience loggers + `read_audit(run_name) -> DataFrame`;
  there is **no edit/delete API**. It logs **actions, not data**: names, counts, ids, statuses, and content
  hashes only — **never measured values, never file contents** (file *names* yes, contents no). Every
  function is defensive — a logging failure **warns, never crashes** the workflow — and the reader tolerates
  malformed lines and unknown (future) `event_type`s. Instrumented seams (small calls, lazy `audit` import
  in `run_manager` to avoid a cycle): **import** (`run_manager.save_lab_dataframe` — rows/mode/columns +
  conversion ids from companions; the app enriches via an `audit_context` with file name/sheet/column
  mapping), **mapping accepted/deleted** (`add_condition_mapping` carries `mapping_status`;
  `delete_condition_mapping_rows`), **validation** (severity counts), **suggestion_table** (per-status
  counts, de-duped per session), **script_run** (name + exit status, from `_run_lab_workflow`),
  **comparison_generated + inclusion** (`scripts/05` — the comparison event *references the Prompt-1
  `comparison_meta.json` hashes* rather than duplicating the stamp; inclusion logs Prompt-4 per-variable
  counts), and **export** (run-CSV / pipeline). The Audit/Help tab gained an **"Audit trail"** expander
  (filter by `event_type`, newest first, download the JSONL — the log is itself the export). The
  `comparison_meta.json` stamp keeps its stale-detection role. Covered by `tests/test_audit.py`
  (append-only, schema, import→map→compare instrumentation, unknown-event-type tolerance).

- **One-click validation report** (Prompt 18 — a self-contained review bundle, no chemistry/ML).
  `flyash_phreeqc_ml/report.py :: build_report(run_name) -> Path` writes
  `experiments/<run>/outputs/validation_report_<ts>/` so an advisor/committee can review *how a
  comparison was produced* **without the app**: a self-contained **`report.html`** (inline CSS, base64
  images — no external refs) with sections for run metadata + provenance (from `comparison_meta.json` +
  `comparison_is_current` — a stale report says **STALE** in the header), measured-data summary +
  overview figures, unit conversions (Prompt-16 provenance) + `verify_conversions`, the mapping table
  with compact **Prompt-6 traces** (matched/missing/conflicting fields), the **Prompt-4** inclusion
  counts + excluded-rows table, the residual table + comparison/residual figures with the sign-
  convention caption, the **Prompt-13** bias table (only when the gate is met), the mapping-status
  summary + per-variable **validity lines stated verbatim**, audit-log warnings, and **Recommended
  next simulations**. Alongside: `measured_clean.csv`, `model_predictions_used.csv`, `mapping_table.csv`,
  `residuals.csv`, `excluded_rows.csv`, **`needed_simulations.csv`** (columns chosen to feed Prompt-11's
  `build_input` — `concentration`/L:S/temperature/time/cover-CO₂ — so export + runner interoperate),
  the copied `audit_log.jsonl`, the figure PNGs, and **`MANIFEST.json`** (per-file SHA-256 + app version
  + timestamp). **Honesty (Prompt-4 wording is the truth):** the header always carries the overall
  validity (`valid` only when *every* comparable variable is valid); whenever it is not `valid` a
  standing banner reads *"This comparison is {status} — it is a workflow check, not model validation."*
  Pure stdlib + existing deps (string-templated HTML; **PDF is future work**). UI: an **"Export
  validation report"** button in Run + Results builds the folder (which logs an audit export event) and
  offers a **zip download**. Report folders are gitignored run outputs. Covered by `tests/test_report.py`
  (builds on the synthetic fixture, MANIFEST hashes verify, STALE header, preliminary/valid banner
  wording, needed_simulations columns ↔ Prompt-11 fields).

- **Generality proven: non-PHREEQC model predictions via a CSV contract** (Prompt 19 — model-agnostic
  comparison, no chemistry/ML). `flyash_phreeqc_ml/parsers/generic_prediction_parser.py` (new) validates
  a documented **model-prediction CSV contract** (`docs/model_prediction_format.md`): required
  `record_key` + `model_name`, prediction columns `pred_pH` / `pred_<X>_mM` named per the dataset
  profile's variables in target units (non-target units convert via the **Prompt-16 registry** with
  tagged provenance), optional metadata matching the profile's mapping fields — with **specific typed
  errors** (`MissingRequiredColumn` / `NoPredictionColumns` / `DuplicateRecordKey` / `BlankRecordKey` /
  `InvalidPredictionValue`; no silent fallback). `ModelProfile` gained `parser_entry_point` + a
  `load_parser()` + `source_kind`; **`PHREEQC_PROFILE`** (pqo) and **`GENERIC_CSV_PROFILE`** are
  registered. **`scenarios.build_scenario_manifest` now consumes either source** (dispatch on a
  `model_name` column → `_manifest_from_generic_predictions`, `state="batch"`), and
  `compare.residuals` gained `predictions_mM_from_manifest` + **`compare_measured_to_manifest`** so the
  comparison is built from the manifest, model-agnostically. The suggestion engine, mapping statuses,
  inclusion, residuals and plots operate on the manifest and **do not import the pqo parser** (pinned by
  `tests/test_manifest_model_agnostic.py`). Data tab: an **"Import model predictions (CSV)"** path
  (same review-before-save as measured import; model name from the file; saved to
  `data/processed/model_predictions.csv` which takes manifest precedence over `phreeqc_results.csv`).
  The **supported-dataset matrix** (`tests/matrix/`, named fixtures + one module per claim a–g: fly-ash+
  PHREEQC, literature separation, hand-computed residuals, reformatted/units import, alternate non-fly-
  ash profile, non-PHREEQC generic prediction end-to-end, **and (g) a second material — red mud — driving
  batch closure → attribution → recovery from its `MaterialProfile`**) pins what "supported" means; the
  **README** claims match the matrix, no more. **Known leak (flagged, not blocking):** the manifest keeps the
  historical column names `phreeqc_record_key` / `phreeqc_<X>_mM` (renaming touches every consumer) —
  they hold whatever model produced the numbers; documented in `docs/model_prediction_format.md`.

- **UI aligned to Import → Validate → Match → Compare → Export + user docs** (Prompt 20 — UI
  reorganization + docs, **no functional/scientific changes**). The five tabs were renamed/re-scoped to
  six: **Start** (`_render_start_tab` → overview + a Help pointer), **Import** (`_render_import_tab` —
  the old Data tab; the data-quality validation summary **moved out** to Validate), **Validate**
  (`_render_validate_tab` — measured-data overview **moved from** Results, the basic data validation
  **moved from** Import, the **Calculation Verification** block + the model raw-outputs viewer **moved
  from** Audit/Help, and the validation & sustainability tables **moved from** Results), **Match**
  (`_render_match_tab` — the old Match PHREEQC; model name now comes from the profile in captions),
  **Compare** (`_render_compare_tab` — run workflow + comparison results, with the **assistant** and
  **surrogate moved in** from Audit/Help; report export + validation tables + measured overview moved
  out), and **Export** (`_render_export_tab` — **report export moved from** Results, **audit trail
  moved from** Audit/Help, plus a new **Help & user guide** rendering `docs/user_guide/`). A shared
  `_next_step_hint` (the Start checklist logic) is surfaced as a **➡️ Next step** line at the top of
  every tab; each tab renders a **specific empty state** when its prerequisites are missing. New user
  docs in **`docs/user_guide/`** (`getting_started` / `input_formats` / `mapping_guide` /
  `interpreting_results` / `data_safety` / `faq`) are rendered in-app from the Export tab. A path-
  display helper `_rel()` makes captions crash-proof when runs live outside the repo (presentation
  only). Smoke-tested at the **AppTest level** (`tests/test_app_tabs_smoke.py`): the full app runs
  end-to-end with no exception and the six tab labels, in the no-run state and against a populated
  synthetic run for each run type.

- **Replicate / uncertainty closeout — SEM, batches, comparison error bars** (Prompt 21 — uncertainty
  visibility, no scientific-logic change). `replicates.replicate_summary` now reports **SEM**
  (`sem_<col> = std/√n`, n = non-null replicate count) alongside std (NaN for n<2, never a fake 0);
  `measured_overview.prepare_overview` group_stats gains `sem`; and the **error-bar toggle (std vs
  SEM)** in the measured-overview and condition-results plots picks which, with a caption stating which
  and n. **n=1 conditions degrade gracefully** — the mean is drawn with **no** error bar (never a zero
  bar) and listed as `n=1`. **Explicit replicate roles** (`replicates.REPLICATE_ROLE_DEFINITIONS`:
  time_point / batch / true_replicate): time is already separated via `condition_key`; **batch** is now
  supported via additive `DatasetProfile.batch_column` / `batch_pattern` (parsed from sample names) +
  `group_by_batch` — when set, `condition_key` appends `_batch<id>` so batches compare separately,
  else they fold into the condition like true replicates (`replicates.batch_id`; `annotate` adds a
  `batch_id` column only when the profile defines batches, so the fly-ash default is unchanged).
  **Uncertainty into the comparison:** `condition_mean_comparison` carries measured `std_<X>` + `sem_<X>`
  through (so plots draw measured error bars) plus a `within_meas_std_<X>` flag (`|residual| ≤ std`,
  None when n<2), with the caption *"a residual smaller than the replicate spread is indistinguishable
  from experimental noise."* How replicates inherit condition-level mappings and how batches differ is
  documented in `docs/mapping_rules.md` (§5.5). Covered by `tests/test_replicate_uncertainty.py` (SEM
  math, batch grouping from synthetic names + a column, std/SEM flowing into the comparison frame,
  n=1 NaN-not-zero degradation).

- **Batch-reaction mass balance — deterministic element closure** (Prompt 23 — **arithmetic only;
  works with zero model/AI/ML present**). `flyash_phreeqc_ml/mass_balance.py` (new, pure) computes, per
  element, `gap = moles_in − moles_liquid − moles_solid` (mmol): `moles_in` = starting solid assay ×
  material mass, `moles_liquid` = measured liquid mM × liquid volume, `moles_solid` = residue assay ×
  recovered solid mass. The **gap is a measured fact** — element *not yet attributed* to liquid or
  solid, with **no mechanism attached**. Honesty rules: all mass→amount conversions go through
  `units.convert` (new `mg→mmol` registry entry, so every derived term carries a `conversion_id` +
  molar mass); a **missing required term → `status=incomplete`** listing the fields (never a partial
  number shown as real); an absent `solid_mass_g` is **assumed = material mass** with the assumption
  recorded (never silently fabricated). `closure(row, element, *, profile, sigmas)` returns
  `{n_in,n_liquid,n_solid,gap,gap_fraction,gap_sigma,uncertainty,status,missing_fields,assumptions,
  provenance}`. **Uncertainty** is propagated (relative-quadrature for products, sum-in-quadrature for
  the gap) only when per-input sigmas exist, else `gap_sigma=None` + `uncertainty="unknown"` (never
  implied zero). `closure_warnings` emits validation-surface issues (negative gap beyond gap_sigma →
  names a likely culprit; gap_fraction > 1.0; implausible over-recovery) — never silent fixes. **Schema
  (additive):** the optional batch block is appended to `config.EXPERIMENTAL_RELEASE_COLUMNS`
  (`material_mass_g`/`material_id`/`reagent`/`reagent_conc_M`/`reagent_volume_mL`/`liquid_volume_mL`/
  `solid_mass_g` + per element `{el}_starting_content`/`{el}_solid_residue`) and the shipped template
  regenerated to match; deliberately **not** added to `EXPERIMENTAL_NUMERIC_COLUMNS` so
  `FLY_ASH_PROFILE.variable_columns` is unchanged, and the parser treats the block as **optional**
  (absence is never an error). `DatasetProfile` gained additive batch fields (`mass_balance_elements`
  empty = OFF — **FLY_ASH_PROFILE does not opt in and is unchanged**). UI: a **Validate-tab expander**
  (per-element closure table with provenance, a liquid/solid/**"unaccounted (not yet attributed)"**
  stacked bar, and the warnings) that renders only when the active profile opts in (else a clear
  empty state). Covered by `tests/test_mass_balance.py` (closure vs hand-computed moles, conversion_id
  on every term, incomplete + assumed-solid-mass handling, negative-gap warning, uncertainty vs a
  hand-check, FLY_ASH unchanged).

- **PHREEQC gap attribution — explain the closure gap** (Prompt 24 — modeled explanation of the
  Prompt-22 measured gap; the measured closure is **immutable input**). `flyash_phreeqc_ml/attribution.py`
  (new) asks PHREEQC *which phases it predicts precipitated* and computes **how much of the measured
  gap that accounts for**, never overwriting the measured numbers. `phreeqc_runner.build_single_input`
  gained **additive** optional extras (byte-identical output when absent — golden test preserved):
  `material_inputs` (dissolved batch material as SOLUTION inputs, flagged), `candidate_phases`
  (profile-declared precipitates added to `EQUILIBRIUM_PHASES`), and `selected_output_elements`
  (per-element `SELECTED_OUTPUT`/`USER_PUNCH` emitting solution + phase moles); `build_input`'s
  OA→1 / PF-GS→2 behaviour is kept via `attribution.build_attribution_inputs`. `attribute_gap(row,
  element, phreeqc_selected_output)` → `{modeled_precipitated_moles, by_phase, modeled_solution_moles,
  gap, gap_explained, gap_unexplained, fraction_explained, status, provenance="phreeqc", measured{…}}`.
  **The `precipitate_in_measured_solid` flag** sets the arithmetic per element (see the Prompt-28
  *filtration correction* below): `True → attribution_to_gap = 0` (precipitate retained in `n_solid`,
  explains the solid's composition not the gap); `False → min(P, gap)` (precipitate passes with the
  filtrate, explains the gap). **The fly-ash default is now `True`** (precipitates retained on the
  0.45 µm filter) — corrected from the original `False`. Profile-configurable
  (`DatasetProfile.precipitate_in_measured_solid` + `mass_balance_candidate_phases` = phase→element);
  documented in `docs/mass_balance.md`. **Status** (parallels mapping status): `closed` /
  `model-explained` / `partially-explained` / `unexplained`, folded into the report's overall validity
  (one source of truth — `report._overall_validity` accepts an `attribution_status` that caps a would-be
  `valid` run at `preliminary` when the budget isn't measured-closed). **UI** (Validate tab): a
  **three-way, never-merged** band display (measured liquid+solid+gap | model attribution by phase |
  unexplained residual) + an honest caption ("model attributes N of M mmol… to calcite; P remain
  unexplained"); **degrades** to the measured gap with "attribution unavailable — configure PHREEQC"
  when the binary/db is absent. **Honesty:** all text is "model attributes" / "predicted to
  precipitate", and no modeled value lands in a measured-labelled field. Covered by
  `tests/test_attribution.py` (arithmetic on a synthetic selected-output, the flag both ways, all four
  statuses, the degrade path, immutability of the measured block, validity feed) with the PHREEQC run
  mocked.

- **AI-assisted literature value retrieval — sourced + quarantined by construction** (Prompt 26 —
  suggestion-only; a literature value can **never** silently enter a calculation). `flyash_phreeqc_ml/ai/literature.py`
  (new) *proposes* sourced literature values (solubility constants, typical element assays, partition
  behaviour) via the Anthropic **web_search** server tool. `propose_literature_values(query) ->
  list[LiteratureCandidate]` (+ wrappers `propose_solubility_constants` / `propose_candidate_phases` /
  `propose_starting_assay` / `propose_partition_behavior`). `LiteratureCandidate` carries
  value/unit/quantity/material/conditions/`conditions_match`/confidence + a `Citation`
  (doi/url/title/authors/year/`supporting_quote`). **Validation in code (before display):** a candidate
  is **dropped** unless it has a supporting quote **and** at least one of doi/url; a DOI is normalised to
  `https://doi.org/<doi>`; the quote is truncated to ≤25 words (copyright). **Quarantine by
  construction:** candidates are written `literature-proposed`, `confirmed=False` to a **separate per-run
  store** `experiments/<run>/outputs/literature_values.jsonl` — never into measured data, the manifest, or
  a comparison CSV. The **single chokepoint** into a calculation is `row_with_confirmed_assays(row,
  records, profile)`, which injects **only** `confirmed=True` starting-assay stand-ins into *blank* cells
  (never overwriting a measured value) and returns a per-column **source badge**; `mass_balance` /
  `attribution` stay pure and never import literature, so an unconfirmed value is simply ignored by every
  calculation (pinned by a test that runs a closure and shows the unconfirmed value does not enter it).
  **Confirmation is deliberate + recorded:** `confirm_value(run, id, *, acknowledge_mismatch=False)` flips
  the record to `literature-confirmed`, retains the citation permanently, and logs `audit.log_event`
  (`literature_confirmed`, carrying the resolvable **DOI/link** + title + year, so the value's downstream
  influence is traceable to the exact paper). A **conditions mismatch** (different material/T/ionic
  strength, flagged by the model) makes `confirm_value` **refuse** unless the second acknowledgement
  (`acknowledge_mismatch=True`, the UI's "I understand this value is from different conditions" checkbox) is
  given — also logged. **System-prompt guards** (stored in the module): only values citable from search
  with a resolvable DOI/URL + a ≤25-word quote; never fabricate from memory; say "no reliable sourced
  value found" rather than guess; always report conditions even when mismatched. **UI** (Validate tab):
  a consent-gated proposer + a **review table** showing the **clickable DOI/URL** (title + year), the
  supporting quote, and the conditions-match warning with the double-ack gate; the mass-balance closure
  **badges** any literature stand-in as "starting assay: literature-confirmed (DOI …), not a measurement".
  Disabled cleanly without `ANTHROPIC_API_KEY` (reuses `import_assist`'s lazy client); per-session
  data-leaves-machine consent before any query. Covered by `tests/test_literature.py` (mocked client +
  search: uncited/quote-less dropped, DOI→doi.org, URL-only accepted, quarantine enforced into
  mass_balance, the mismatch double-ack gate, the "no value found" path, disabled-without-key).

- **Per-element recovery report — present → where it went → confidence** (Prompt 25 — pure templating
  extension of `report.build_report`, no new deps). Adds an **Element recovery** section that, *per
  element per condition*, states the **starting amount** (provenance-flagged — measured assay vs a
  Prompt-26 `literature-confirmed` stand-in, with the **DOI/link inline**), **measured liquid** + **solid**,
  the **closure gap ± gap_sigma** (Prompt 22), the **PHREEQC attribution by phase** + **unexplained
  residual** (Prompt 24), and a **recovery status** (the four attribution statuses). Helpers mirror the
  existing ones (`_recovery_records` / `_recovery_table` / `_recovery_summary`, alongside
  `_inclusion_by_variable` / `_needed_simulations`); a generated **narrative** per element ("Of N mmol Ca
  initially present (measured assay), X% in liquid, Y% in solid; Z mmol unaccounted, of which the model
  attributes W mmol to calcite, leaving V mmol unexplained"). Because the report has **no live PHREEQC**,
  attribution is *unavailable* offline (whole gap unexplained) unless a parsed selected output is passed
  to `_recovery_records(selected_outputs=...)` — the path the tests use to reach model-explained / partial.
  New bundle CSV **`element_recovery.csv`** (every term + `starting_provenance` + `starting_citation`) and
  a **summary table sorted by unexplained fraction** (the "where the tool's knowledge is weakest" view).
  **MANIFEST.json** gains `recovery_classification`, tagging each term **measured / derived / modeled /
  literature-confirmed** (n_in is measured **or** literature-confirmed per row — the `starting_provenance`
  column is authoritative). **Honesty:** the section **reuses `_overall_validity` + `_validity_class`** —
  an element balance is only "explained" when `closed` or `model-explained` within uncertainty;
  `partially-explained` / `unexplained` carry the standing caution and an open element budget caps a run
  at `preliminary`, never `valid`. The feature is **profile-gated** (FLY_ASH_PROFILE does not opt into
  mass balance, so the live app shows the empty-state note). Covered by `tests/test_report.py` additions
  (each status reached, provenance flags render, the literature DOI/link shows, manifest classification,
  summary sort order).

- **Batch + recovery across materials — `MaterialProfile`** (Prompt 28 — additive profile layer, no
  package rename). Pushes material/reagent/phase specifics into the profile system so the Prompt-22–25 +
  Prompt-27 batch chemistry runs for **any** material, not just fly ash. `profiles.MaterialProfile`
  (frozen) declares `material_id` / `display_name`, `relevant_elements`, `mass_balance_elements`,
  `candidate_phases` (phase→element), `precipitate_in_measured_solid`, `default_reagents`, and a
  provenance-flagged `declared_assay` (`AssayValue`: `measured` / `literature-confirmed` usable;
  `literature-proposed` **quarantined** — `is_usable` False — until confirmed, satisfying the Prompt-24/26
  rule for a new material). `DatasetProfile` gains an additive `material` field, and module-level
  **resolvers** (`mass_balance_elements` / `candidate_phases` / `precipitate_in_measured_solid` /
  `default_reagents` / `usable_declared_assay`) read the material first, else the legacy DatasetProfile
  batch fields. `mass_balance`, `attribution`, the `report` recovery section, and `incompleteness_model`
  now read **elements/phases/flag from the active profile via these resolvers** — **none hard-codes a
  fly-ash element or phase** (new guard `tests/test_material_profile_agnostic.py`, mirroring
  `test_manifest_model_agnostic.py`). Ships `FLY_ASH_MATERIAL` (Ca/Si/Al/Fe/Na/K, NaOH; closure stays
  OFF → unchanged) and a second-material stub `RED_MUD_MATERIAL` / `RED_MUD_PROFILE` (Ti/V/Fe/Al + REE,
  anatase/rutile/hematite/… phases, **opposite** filtration flag, different reagents) to prove the
  abstraction. A **no-code profile-creation path** (`dataset_profile_from_spec` /
  `material_profile_from_dict` / `load_dataset_profile`; JSON always, YAML if PyYAML present) lets a
  researcher define a material in one file — documented in `docs/defining_a_material.md` with a shipped
  example `docs/examples/red_mud_material.json` (a literature-proposed declared assay is rejected from any
  calculation; a literature provenance without a citation is rejected at load). Ti/V molar masses added to
  `units.MOLAR_MASSES`. Matrix claim **(g)** `tests/matrix/test_g_second_material.py` runs red-mud batch
  → closure (hand-computed Ti moles) → mocked attribution (anatase, material's precipitate flag) →
  recovery section, asserting **zero fly-ash leak** (only Ti/V/Fe/Al rows). Covered also by
  `tests/test_profiles.py` (resolvers, factory, JSON spec round-trip from disk, quarantine).

- **Filtration-convention correction — per-element retained/passes/uncertain** (Prompt 28b — science
  correction; *inverts the attribution arithmetic*). The physical fly-ash protocol **retains** secondary
  precipitates on the 0.45 µm filter, so they are part of the measured solid residue — the
  `precipitate_in_measured_solid` default was flipped **`False → True`** (a retained precipitate explains
  the **solid's composition, not the gap** → `attribution_to_gap = 0`; the old `False` *over-credited*
  precipitates with closing the gap, so the unexplained residual is now generally **larger** — the correct
  result for this protocol, not a regression). Retention is **not uniform across elements**: Si/Al
  (colloidal silica / aluminosilicate gels) and Fe (nanocolloids) can pass a 0.45 µm filter, so a
  **per-element override** mechanism was added — `DatasetProfile.precipitate_in_measured_solid_overrides`
  (`{element -> True|False|"uncertain"}`) + a `filter_cutoff_um` field, resolved by
  `profiles.precipitate_in_measured_solid_for(profile, element)`. The fly-ash profile ships `True` with
  **Si/Al/Fe = `"uncertain"`** and `filter_cutoff_um = 0.45` (knowing the cutoff does **not** resolve it —
  0.45 µm is exactly where these colloids may pass; needs a filtrate-vs-ultrafiltrate check). An
  `"uncertain"` element is credited **0** (conservative — never over-credits) but **flagged**:
  `attribute_gap` returns `filtration_status` / `filtration_uncertain` / `gap_explained_if_passes`,
  surfaced in the caption, the recovery narrative, `element_recovery.csv` (`filtration_status` column), and
  a `⚠ filtration uncertain` badge in the report HTML. `docs/mass_balance.md` no longer asserts
  "CONFIRMED" — it states the True default *per the experimenter's procedure* and leaves a blank
  `confirmed by: __ (date __)` line + the ultrafiltrate caveat. Covered by `tests/test_attribution.py`
  (retained→0, per-element mix, uncertain→0+flag+alternative, fly-ash uncertain defaults),
  `tests/test_report.py` (recovery surfaces the uncertain flag), and `tests/test_profiles.py` (spec parses
  overrides + cutoff, rejects bad values).

- **Safe AI configuration + client layer** (foundation — opt-in AI made visible/configurable; no
  result-path change). `flyash_phreeqc_ml/ai/config.py` (new) is the **single AI-config authority**:
  provider/model/key detection and an `AIConfig` snapshot that holds **no key** (only presence + source),
  with documented precedence — API key **env (`ANTHROPIC_API_KEY`) wins over `st.secrets`**; model
  `arg > UI override > ANTHROPIC_MODEL env > secret > DEFAULT_MODEL`. Pure + import-safe (lazy
  `streamlit`/`anthropic`, never raises; `st.secrets` is read **only under a live Streamlit runtime**, so
  scripts/tests stay env-only). `ai/client.py` (new) is the **key-safe client wrapper**: builds an
  Anthropic client only when enabled and returns a structured `ClientResult` (`ok` / stable error code /
  **key-free** message), never raises, **never exposes the key**. `import_assist.is_enabled` / `_model` /
  `_resolve_client` now **delegate** to this layer (assistant + literature transitively reuse it). The
  sidebar gained a **🤖 AI settings** panel (status: enabled/disabled, provider, model, key-detected
  yes/no + source — never the key, SDK available) with provider/model selectors; **no key entry in the
  UI** (env/secrets only). **No hard-coded keys; the key is never shown or logged.** AI stays
  suggestion/interpretation-only and **off the result path** (pinned by `tests/test_ai_boundary.py`).
  Local + Streamlit-Cloud-secrets setup + precedence in `docs/ai_configuration.md`. Covered by
  `tests/test_ai_config.py` + `tests/test_ai_client.py`.

- **Natural-language simulation planner (Simulate tab)** (a **planning layer only** — **no PHREEQC
  execution**, off every result path). New `flyash_phreeqc_ml/simulation/` package: `scenario_schema.py`
  (dataclasses `MaterialInput`/`LeachantInput`/`ExperimentProcess`/`TargetOutputs`/`MissingInput`/
  `Assumption`/`SimulationScenario`/`ScenarioParseResult` + flat serialization + the plan-only caveats),
  `safety.py` (**deterministic** missing-field + scientific-warning analysis — the caveats come from code,
  **not** the AI), `rule_parser.py` (non-AI regex fallback extractor), `matrix.py`
  (`build_simulation_matrix(scenario, ranges=…)` — a plan table where every row is `status='plan_only'`;
  range-expansion ready). `ai/scenario_parser.py` (new) extracts a scenario via the shared key-safe AI
  client (strict-JSON, validated; invalid output → controlled error) and orchestrates **AI-when-consented,
  else the rule-based fallback**. The **Simulate** tab is the forward-looking core: describe → AI/rule
  extract → review missing/assumptions/warnings/confidence → edit/confirm → choose a **simulation
  strategy** (single scenario + small parameter sweep are real via the matrix `ranges`; large-batch /
  adaptive / surrogate-assisted are shown **disabled/future**) → generate a **plan matrix** (download-only
  CSV). **It never runs PHREEQC, never overwrites measured data, never becomes verified data, and never
  affects mapping/residuals/validation/comparison** (pinned by `tests/test_ai_boundary.py`). Docs in
  `docs/simulation_planner.md`. Covered by `tests/test_scenario_parser.py` + `tests/test_simulation_matrix.py`.

- **App identity renovation — AI-assisted simulation & validation platform** (UI/docs presentation only;
  **no scientific/logic change**). Repositioned the app from a "Class C fly ash + PHREEQC mapping/validation
  app" into a general **AI-assisted geochemical / material-leaching simulation & validation platform** with
  **Simulate as the forward-looking core**. New platform title/hero; a Start-tab **three-mode product
  panel** (Mode 1 Simulate / Mode 2 Validate / Mode 3 Learn & Improve) + a describe→simulate→validate→learn
  stepper; the measured-vs-model mapping/comparison reframed as the **Validation module** (the current
  strongest workflow, not the whole app); general material/leachant/model wording at the high level
  (fly-ash examples / OA-PF-GS cup-cover / mass-balance / PHREEQC database+template specifics kept where
  genuinely specific); **tabs reordered (Simulate 2nd) + relabeled** → **Start · Simulate · Import Data ·
  Validate · Match · Compare Results · Export**; README + user-guide docs updated. The only package edits
  are presentation **string constants** (`PLAN_ONLY_LABEL` / `NON_PREDICTION_NOTE` in `scenario_schema.py`
  + one warning in `safety.py`) — the machine status value (`STATUS_PLAN_ONLY="plan_only"`) is **unchanged**.
  Verified by a 4-lens adversarial review (logic-untouched / boundaries / scientific-honesty all pass).
  `tests/test_app_tabs_smoke.py` updated to the seven-tab order + a renovated-identity assertion.

- **pH-graph provenance + Simulate plan-only clarification** (UI labels only; no science change). Audited
  every pH graph and made explicit that the Simulate tab is **plan-only and wired to no graph** — it renders
  only tables (scenario + plan matrix), runs no model, writes no file. Added a Simulate-tab note ("Changing
  simulation-plan values does not update result graphs until a deterministic model is executed"), a
  `_png_provenance_caption` helper stamping every **static** result PNG (`measured_vs_phreeqc.png` /
  `residuals_by_sample.png` / the PHREEQC-only `pH.png`) with its **source path + generated time + type +
  "regenerated only by re-running the workflow, not by the Simulate tab"**, and "not affected by the Simulate
  tab" notes on the **live** measured-only / measured-vs-model figures. `_read_csv` was already mtime-keyed
  (no stale-cache bug) — the graphs were simply unconnected to Simulate by design. Covered by
  `tests/test_ph_graph_provenance.py`.

- **Deterministic PHREEQC input previews (Simulate Step 7)** (planning layer only — **no PHREEQC
  execution**; AI writes no input). After a confirmed plan, `flyash_phreeqc_ml/simulation/phreeqc_input_builder.py`
  (new) templates a reviewable **draft `.pqi`** per scenario: `build_phreeqc_input_preview(scenario, *,
  scenario_id, material_profile)` / `build_previews_for_matrix(...)` → a `PhreeqcInputPreview` (scenario_id,
  phreeqc_input_text, template_type, status, warnings, assumptions, unsupported_features). Conservative
  templates for **water / NaOH / HCl** (each `SOLUTION` block states its assumptions: full dissociation,
  charge-balanced pH, equilibrium-only); every input carries comment lines stating it is a draft preview,
  PHREEQC has not been run, it needs expert review, the database choice matters, material composition
  controls quality, kinetics are not modeled, and precipitation depends on the phase-list + database.
  **Status** (deterministic precedence): `unsupported_leachant` → `missing_required_field` →
  `needs_material_composition` → `template_warning` (water/HCl are preview-only — the on-demand runner
  templates NaOH only) → `ready_for_review` (NaOH + a *usable* assay); `draft_only` for a generic material.
  **It never runs PHREEQC** (imports no `subprocess`/`os`/`phreeqc_runner`), **writes no files** (in-memory
  + downloadable `.pqi` only), and **never invents material composition** — composition is included only from
  a profile's **usable** declared assay (`measured`/`literature-confirmed`); a quarantined
  `literature-proposed` assay is ignored (so fly ash, shipping no committed assay, honestly lands at
  `needs_material_composition`). **AI writes no PHREEQC input** — the LLM only extracted the scenario;
  deterministic code templates the text. Simulate **Step 7** adds the preview (button → scenario selector →
  `.pqi` code block → warnings/assumptions/unsupported panels → `.pqi` download) with the standing "Input
  preview only — PHREEQC has not been run yet" label. The existing `phreeqc_runner` and all result-path logic
  are **untouched**. Docs in `docs/simulation_planner.md`; covered by `tests/test_phreeqc_input_builder.py`;
  boundaries (no execution, no AI import, not on the result path) pinned by `tests/test_ai_boundary.py`.

- **Material profile / composition manager (Simulate Step 7)** (Simulate-only helper, no chemistry on
  the result path). `flyash_phreeqc_ml/materials/` (new package: `profile_schema.py` + `profile_validation.py`)
  lets a user **provide / review / confirm** a material's bulk composition four ways (manual oxide/element
  table, paste `species value`, upload `.csv`/`.xlsx`, or consent-gated **Literature (AI)** that proposes
  *unverified* values only). Composition can be **oxide wt% / element wt% / mg·kg⁻¹ / mol·kg⁻¹** → all
  converted to element wt% (oxides via gravimetric factors, e.g. CaO → Ca×0.715) and validated. A profile is
  **draft until confirmed**; a literature-sourced one is `literature_unverified` (needs a second
  acknowledgement). **Only a confirmed profile feeds the input preview** (composition is never invented);
  it exposes a `usable_assay` interface the preview builder duck-types. Session-only (writes nothing), off
  the result path. Docs `docs/material_profiles.md`; covered by `tests/test_material_profile.py`; boundary in
  `tests/test_ai_boundary.py` (materials manager imports no science/planner; result path + planner import no
  materials manager).
- **Material release / source terms (Simulate Step 7b)** (deterministic source-term templating, no result
  path). `flyash_phreeqc_ml/simulation/source_terms.py` (new) converts a confirmed profile + a **user-chosen,
  reviewable release model** into PHREEQC source-term blocks: `MODE_NONE` (default — assay stays comment-only,
  **nothing dissolves**, warns predicted totals ≈ 0), `MODE_GLOBAL` (one fraction), per-element fractions,
  `MODE_LITERATURE` (sourced — **blocked until confirmed**), `MODE_MEASURED_LIQUID` (measured concentrations as
  input). Released elements are added as **oxides via a `REACTION` block** (charge-safe) with the `SOLUTION`'s
  `-water` set to the liquid volume → the real L/S (validated against real PHREEQC 3.8.6). It **never invents a
  fraction, never defaults to 100%**; release fractions are **USER ASSUMPTIONS**, not measured truth, and this
  is an **equilibrium** source term (kinetics are future work). Pure (imports only `materials.profile_schema`);
  no AI, no executor. Docs `docs/material_release.md`; tests `tests/test_source_terms.py` (incl. an optional
  real-PHREEQC test); boundary in `tests/test_ai_boundary.py`.
- **Database compatibility + candidate phase templates (Simulate Step 7c)** (pure text inspection, no result
  path). `flyash_phreeqc_ml/simulation/database_compatibility.py` + `phase_templates.py` (new) inspect the
  *configured* thermodynamic database: `build_report(expected_phases=…)` detects the family (phreeqc / llnl /
  wateq / cemdata / unknown, from name + header) and reports **which template phases it actually defines**, so
  the input builder adds **only available phases** to `EQUILIBRIUM_PHASES` and lists the rest as
  `# SKIPPED (NOT added)` comments + warnings — **never invents a phase**. `phase_present` uses a
  whitespace/EOL anchor (`Calcite` ≠ `Cal`; phases ending in `)` like `SiO2(a)` match). Small reviewed
  templates: aqueous-only (default) / fly-ash cementitious / red mud / generic. The Match-tab runner's CEMDATA
  gate (`phreeqc_runner.is_cemdata_compatible` / `database_defines_phases`) now **delegates** here, so its
  integration test **skips** on `phreeqc.dat` instead of failing. Pure; no AI/executor/result-path. Docs
  `docs/database_compatibility.md`; tests `tests/test_database_compatibility.py` (tiny fake `.dat` in tmp — no
  real DB committed); boundary in `tests/test_ai_boundary.py`.
- **Safe PHREEQC execution (Simulate Step 9)** (gated; off every result path). `flyash_phreeqc_ml/simulation/phreeqc_executor.py`
  (new) makes the reviewed `.pqi` runnable: `check_availability()` / `execute_preview(preview)` / `parse_outputs(result)`,
  **structured + never-crashing** (statuses `not_run` / `success` / `failed` / `phreeqc_missing` / `timeout`;
  parse status separate). It runs the **user-supplied PHREEQC CLI** (`PHREEQC_EXE` + `PHREEQC_DATABASE` from the
  environment; default 120 s timeout via `PHREEQC_TIMEOUT_S`), parses pH / pe / element totals (mM) / saturation
  indices, and **writes only to a safe workspace** `outputs/simulations/` (an `assert_safe_workspace` guard
  forbids `data/raw`, `data/processed`, and the source tree). It runs **only on an explicit confirmed click** —
  never automatically — and is **off the result path** (no AI, no comparison/mapping module). Outputs are
  **simulation results, not validated predictions**. Docs `docs/phreeqc_execution.md`; tests
  `tests/test_phreeqc_executor.py` (real run mocked; one optional integration test); boundary in
  `tests/test_ai_boundary.py`. *(Verified end-to-end against a from-source PHREEQC 3.8.6 build with `phreeqc.dat`.)*
- **Small-sweep batch execution + Simulate plots (Simulate Step 9)** (prototype-scale orchestration, off the
  result path). `flyash_phreeqc_ml/simulation/batch_executor.py` (new) runs a confirmed simulation matrix
  scenario-by-scenario: `run_batch(previews, max_scenarios=DEFAULT_MAX_SCENARIOS=20, on_progress=…)` (capped at
  20 — excess **dropped with a flag, never silently**; one failed scenario never stops the batch),
  `build_result_table` (one row per scenario: status / pH / pe / `<El>_mM` / key SIs / runtime / warnings),
  `detect_sweep_axis` + `sweep_plot_frame` (plot-ready `(x,y)` for pH/element vs the swept parameter). Imports
  only the safe executor + pandas. The Simulate tab draws **pH-vs-sweep** + **element-vs-sweep** plots only
  because executed results exist. Tests `tests/test_batch_executor.py`; boundary in `tests/test_ai_boundary.py`.
- **Simulation run registry + provenance (Simulate save)** (provenance store, off the result path).
  `flyash_phreeqc_ml/simulation/run_registry.py` (new) saves a whole Simulate execution with its full
  provenance chain — experiment text → parsed scenario → assumptions → material profile → generated input →
  executable/database **paths** → output files → parser status → warnings. `build_run_record(...)` +
  `SimulationRunRegistry` (`save_run` / `list_runs` / `load_run` / `export_zip` → `outputs/simulation_runs/<id>/`
  with `run_metadata.json` + CSVs + copied `.pqi` inputs). Every record is labelled a **simulation run, not
  validated**; it **stores no secrets** (no API key, not the raw AI response) and never writes to `data/raw`,
  `data/processed`, or a validation CSV. The record carries optional `refinement` (Prompt: refined sweep) and
  `target_match` (inverse search) provenance blocks. Docs `docs/simulation_runs.md`; tests
  `tests/test_run_registry.py`; boundary in `tests/test_ai_boundary.py`.
- **Simulation strategy / ranking + refined sweep (Simulate)** (deterministic optimisation over EXECUTED
  outputs, off the result path). `flyash_phreeqc_ml/simulation/strategy.py` (new) lets a user say what to
  optimise, then **ranks already-executed** results (it **runs nothing**): `parse_objective(text)` (rule-based —
  maximize/minimize/target-pH/avoid-pH/selectivity/minimize-reagent/weighted), `rank_results(table, objective)`
  (per-metric [0,1] score → weighted rank, missing-metric warnings, tradeoff notes, driving metric), and
  `suggest_refined_sweep` / `refined_sweep_plan` (a cautious, **physical**, **capped** next sweep — extend
  lower/upper, refine internal, narrow failures — plan-only). Ranking is *"over model predictions, not
  validation"*. Imports only `re`/`dataclasses`/`pandas` (no executor / AI / result path). Docs
  `docs/simulation_strategy.md`; tests `tests/test_strategy.py`; boundary in `tests/test_ai_boundary.py`.
- **Target matching / inverse simulation search (Simulate Step 10)** (deterministic inverse search, off the
  result path). `flyash_phreeqc_ml/simulation/target_matching.py` (new) works **backwards from a desired
  result**: `parse_target_spec(text)` (deterministic — pH **range** / target **value** / **maximize** /
  **minimize** / hard **constraint**, combinable, e.g. *"maximise Si while keeping Fe below 0.1 mM"*),
  `build_search_grid(scenario, params, max_scenarios=20)` (Cartesian over leachant concentration × **release
  fraction**, **capped at 20**, excess dropped with a flag), and `score_results(spec, table)` (objective metrics
  each scored [0,1]; **constraints gate feasibility**; ranked **feasible-first then by score**; a metric whose
  column is absent is **warned, never fabricated**; returns the best candidate + per-row breakdown). A match is
  **inverse search over model predictions, not validation** (depends entirely on the chosen ranges; release
  fractions stay assumptions). Pure (imports only stdlib + pandas — **no executor / AI / result path**;
  execution happens via the batch executor on an explicit UI click). Saved via
  `run_registry.build_run_record(..., target_match=provenance)`. Simulate **Step 10** UI (`_render_target_matching`
  + editor / per-candidate previews / gated run / ranked table / best / breakdown / own save). Docs
  `docs/target_matching.md`; tests `tests/test_target_matching.py`; boundary in `tests/test_ai_boundary.py`.
  *(Verified end-to-end against real PHREEQC 3.8.6.)*
- **UI modularization — `app.py` → `ui/` package** (presentation/structure only; **behaviour byte-identical**).
  `app.py` was ~6,656 lines (the whole seven-tab Streamlit workflow + ~165 helper/render functions). It is now a
  **thin entry point** (~160 code lines: the `sys.path` bootstrap, the run-management **sidebar**, page config +
  hero, and the `st.tabs([...])` dispatch to `ui.<tab>.render(...)`). The render functions moved **verbatim**
  into a `ui/` package: one module per tab (`ui/start_tab.py` / `simulate_tab.py` / `import_tab.py` /
  `validate_tab.py` / `match_tab.py` / `compare_tab.py` / `export_tab.py`, each keeping its original
  `_render_<tab>_tab` function and exposing it as `render`), plus shared `ui/state.py` (constants, paths, cached
  readers), `ui/common.py` (shared render helpers), and the existing `ui/formatters.py`. The partition was
  computed by **reachability** from the seven tab roots (helper used by one tab → that tab; by ≥2 tabs/sidebar →
  state/common), giving an **acyclic DAG** (tabs → state/common/formatters; science never imports `ui`).
  **Verified behaviour-preserving:** every function body + constant byte-identical, all 101 session-state/widget
  keys + 162 widget labels identical, pyflakes-clean (no undefined names), all tabs render via AppTest. Docs
  `docs/refactor_plan.md` (UI architecture + how to add a UI section safely); boundaries pinned by
  `tests/test_ui_modularization.py`; the source-scan test `tests/test_ph_graph_provenance.py` now scans the
  `ui/` layer. **No scientific/PHREEQC/result-path logic changed.**
- **README + architecture docs demo-readiness pass** (docs only; no code/science change). The README was
  rewritten as a presentable research-prototype doc — identity, current capabilities, **what it cannot yet
  claim**, a **Simulate-vs-Validate/Compare** distinction, install / launch, **AI / PHREEQC / CEMDATA18**
  configuration, a **5-minute demo**, a **professor-facing** section, an explicit **Limitations** section
  (release fractions are assumptions; database controls phases; `phreeqc.dat` weak for cementitious; CEMDATA18
  not shipped; outputs not validated until compared; kinetics + large-scale search are future work), **data
  safety + generated-output folders**, and an **architecture** note. Fixed stale claims (Simulate now has
  **gated execution**, not "never runs PHREEQC"; the `CO2_condition` cup-cover vocabulary `OA/PF/GS +
  atm_CO2/low_CO2/no_CO2`, not legacy `open/sealed`; Calculation Verification lives in **Validate**, not a
  removed "Audit/Help" tab). `docs/refactor_plan.md` extended with the science / AI-suggestion-only / `outputs/`
  layering.

- **AI agent orchestration layer — the Assistant tab** (AI plans/clarifies/explains; deterministic code +
  user confirmation do everything scientific). New `flyash_phreeqc_ml/agent/` package turns the manual
  Simulate flow into a conversation: **conversation state → LLM proposes ONE structured action → policy
  check → explicit user confirmation (for execution/save) → deterministic tool runs → updated state →
  reply**. Modules: `agent_state.py` (state machine + a deterministic, **correction-aware merge** of a
  natural reply — only fields the reply explicitly states, later corrections win, no fabricated
  temperature; provenance trace), `agent_actions.py` (the 19-action vocab + `AgentAction` + `parse_action`
  that **strips `FORBIDDEN_ARGUMENT_KEYS`** like `phreeqc_input_text`/`composition`/`release_fraction`/`ph` +
  per-action `ACTION_SPECS` metadata: risk / confirmation / allowed-domains / preconditions), `domains.py`
  (rule-based **domain classification** + the engine map `{leaching_geochemistry → PHREEQC}` only +
  per-domain **planning support**), `agent_prompts.py` (system prompt forbidding invented chemistry + a
  **grounded** per-turn prompt showing the deterministic state), `tool_registry.py` (binds each action to an
  **existing** deterministic backend fn — builder / executor / batch / matrix / strategy / target-matching /
  run-registry — **imports no AI**), `agent_policy.py` (the gate + the deterministic fallback planner —
  **imports no AI, no executor**), `agent_orchestrator.py` (the loop; the **only** module that touches AI,
  via the key-safe client mirroring `scenario_parser`). **Safety invariants:** the LLM only *proposes* —
  execution/save are **parked** and run only via `confirm_pending_action` (the UI's "Yes, run it" button or
  an unambiguous affirmative reply; the model can't propose+confirm in one turn); **PHREEQC is blocked for
  non-leaching domains** (the domain gate runs first, so `confirmed=True` can't bypass it) and **for
  missing-composition runs**; **AI never writes PHREEQC input** (the deterministic builder templates it from
  the scenario; injected text is stripped); **numbers come from tools, not the model**; simulation ≠
  validation (every result carries the not-validated caveat). With no API key a **deterministic planner**
  drives the same flow. Provenance: `run_registry.build_run_record` gained an additive `agent_provenance`
  dict (transcript summary + action trace + confirmed assumptions + not-validated label) — **never raw model
  responses, secrets, or measured data**; mirrors the `refinement`/`target_match` additive pattern, no
  scientific field changed. UI: `ui/assistant_tab.py` (chat + cards + confirm buttons + advanced expanders;
  consent-gated). Docs `docs/assistant_agent.md`; behaviour pinned by `tests/test_agent.py`, boundaries by
  `tests/test_ai_boundary.py` (agent pure modules import no AI/executor; tool registry imports no AI; no
  result-path module imports the agent). **The agent is off the scientific result path** — it orchestrates
  the Simulate side only.

- **Materials Research Assistant UI redesign** (presentation + agent planning-only responses; **no
  scientific / PHREEQC / result-path change**). Reframed the app from a PHREEQC validation dashboard into a
  broad, chatbot-first **Materials Research Assistant** with PHREEQC as the *first* executable engine.
  Hero/identity → "Materials Research Assistant" (`ui/state.PRODUCT_NAME`/`PRODUCT_SUBTITLE`); chips frame
  the executable engine (leaching/geochemistry via PHREEQC) + planning-support domains + "modular — more
  engines can be added". **Tabs reduced 8 → 7** — the **Start** tab was removed (its overview folded into the
  Assistant homepage; `ui/start_tab.py` deleted) and the rest grouped as **Advanced Mode**:
  `Assistant · Advanced Simulate · Import Data · Validate · Match · Compare · Export` (`Simulate`→`Advanced
  Simulate`, `Compare Results`→`Compare`; each advanced tab gets an `app_ui.render_advanced_mode_note`
  caption). The **Assistant homepage** (`ui/assistant_tab.py` rewritten) adds example **prompt chips**, four
  live cards (Experiment so far · Domain & engine · Still missing · **Next action**), an **engines &
  capabilities** panel (available now / planning support now / future, from `domains.engine_status`), a
  compact **7-step flow**, and a **planning-support panel** for unsupported domains; technical detail
  (scenario JSON, domain classification, policy decision, input preview, database report, result table,
  provenance trace) is under expanders. **Better unsupported-domain behaviour** (the only agent logic change,
  message/metadata only): `domains.PLANNING_DOMAIN_INFO` / `planning_support` / `data_template_columns` +
  `FUTURE_ENGINES`; `planning_only_message` now offers to **structure the plan / build a data template /
  identify missing variables**, suggests the domain's **response variables** (e.g. composite: compressive /
  flexural strength, density, water absorption, toughness) and the inputs a **future model** needs, and the
  data template becomes a **domain-aware data-collection template** (`_tool_create_validation_template`) —
  **never a fake simulation**. Verified by a 2-lens adversarial review (unsupported-cannot-simulate /
  result-path-untouched, both pass, 0 bugs) + `tests/test_app_tabs_smoke.py` (broad-identity, 7-tab) and
  `tests/test_agent.py` (planning actions, domain-aware template, PHREEQC-only-for-leaching). README +
  `docs/assistant_agent.md` updated (broad identity, PHREEQC = first engine, planning-only domains, future
  LangGraph-style orchestration note).

- **Assistant-first redesign — functional minimalism + four-section nav** (UI/UX + design system; **no
  scientific / PHREEQC / result-path change**). Replaced the **seven top-level tabs** with a simple
  **four-section sidebar navigation** so the assistant is unmistakably the main workspace, not a peer of the
  technical tabs: **Research Assistant · Projects / Runs · Data & Validation · Engine Settings** (section
  constants in `app.py`; a `st.sidebar.radio(key="nav_section")`). **No workflow was removed** — every
  existing render is still dispatched: Research Assistant = `assistant_tab` + an **Advanced Mode** expander
  holding the full `simulate_tab`; Projects / Runs = `export_tab`; Data & Validation = `st.tabs` over
  `import_tab`/`validate_tab`/`match_tab`/`compare_tab`; Engine Settings = new **`ui/engine_settings.py`**
  (engine roadmap via `app_ui.render_engine_cards(domains.engine_status())`, PHREEQC executable/database
  status, the **AI provider/model panel moved out of the sidebar** — model override persists via the
  module-level `ai_config` runtime overrides — and the future-engine architecture note). **`ui/assistant_tab.py`
  rewritten as a clean two-column page** (`st.columns([2,1])`): left = chat history + example prompt chips +
  confirm buttons + planning support; right = card-based side panel (**Experiment summary · Domain & engine ·
  Missing details · Current assumptions · Next recommended action**) via bordered `st.container`s; all
  technical readouts (scenario JSON, policy decision, generated PHREEQC input, database report, release model,
  raw result table, provenance) stay **hidden under collapsed expanders** (`app_ui.advanced_expander`), so the
  default surface is conversational. **Design system** (`app_ui.py` + new **`.streamlit/config.toml`**):
  Apple/Squarespace-inspired light theme — `#F5F5F7` background, white `#FFFFFF` cards, `#007AFF` accent,
  rounded corners, minimal borders, system font, design tokens in `:root`; `STATUS_STYLES` palette refreshed
  (success `#34C759` / warning `#FF9500` / danger `#FF3B30`); `st.metric` / expanders / bordered containers
  styled as white cards. **AI framework direction** (docs only): new **`docs/ai_architecture.md`** + a
  LangGraph-compatibility note in `agent/__init__.py` — the loop is already a state machine (`AgentState` =
  graph state, actions = nodes, `agent_policy` = edge function, `domains.EXECUTABLE_DOMAINS` = plugin-engine
  registry), so it can become a LangGraph-style orchestrator (+ RAG / ML-surrogate / simulation-engine /
  validation agents) without changing the safety model; **no LangGraph dependency added**. `app.py` stays thin
  (~210 code lines; only top-level func is `_render_run_sidebar`; AI-settings moved to the section).
  Agent message strings updated for the new UI ("Simulate tab" → "Advanced details / Advanced Mode").
  Verified by a 4-lens adversarial review (no-science-change / nothing-removed-all-reachable /
  unsupported-no-simulate / design-and-identity — **all pass, 0 bugs**) + `tests/test_app_tabs_smoke.py`
  rewritten for the 4-section nav (each section renders no-run / populated / per run-type; advanced workflows
  reachable; advanced details hidden by default; broad identity) and `tests/test_ui_modularization.py`
  (`engine_settings` added, app.py top-func set tightened). README updated (four-section nav table, Research
  Assistant workspace, future plugin-engine architecture).

- **Dark research-cockpit makeover — seven-section nav + design system** (UI/UX + presentation + agent
  message/routing data; **no scientific / PHREEQC / execution / source-term / database / ranking /
  validation logic change**). Reworked the app into a **dark, Apple/Squarespace-inspired** assistant-first
  cockpit. **Design system:** `.streamlit/config.toml` switched to a dark base theme; `app_ui.py` CSS +
  `STATUS_STYLES` rewritten to the dark palette (bg `#0E1014`, sidebar `#090B0F`, main `#12151B`, card
  `#191D24`, border `#2D333D`, accent `#6EA8FE`, success `#4FC58A` / warning `#D8A03D` / danger `#E06C75`;
  design tokens in `:root`, dark cards/sidebar/buttons/chat/expanders, a hidden-dot left-rail nav). **Nav
  expanded 4 → 7 sections** (`st.sidebar.radio(key="nav_section")`): **Assistant · Workspace · Results ·
  Data & Validation · Projects · Engine Library · Settings**. Mapping (no workflow removed): **Assistant** =
  `assistant_tab` (the chat is no longer a peer of "Advanced Simulate"); **Workspace** = `simulate_tab` (the
  full manual builder, promoted out of an expander); **Results** = new `ui/results.py` (clean read-out of
  the agent state's latest prediction — pH / element totals / target match / sweep plots, labelled *model
  prediction only, not validated*, raw table under an expander); **Data & Validation** = Import/Validate/
  Match/Compare sub-tabs; **Projects** = `export_tab`; **Engine Library** = new `ui/engine_library.py` (the
  modular engine registry — PHREEQC executable, fly-ash mature demo, planning domains, future engines);
  **Settings** = new `ui/settings.py` (AI provider/model + PHREEQC status + dev-mode + future-architecture).
  `ui/engine_settings.py` was **split** into `settings.py` + `engine_library.py` (functionality preserved,
  not removed); `DEV_MODE` moved from the sidebar into Settings (read via `st.session_state["dev_mode"]`).
  The **Assistant** right panel was refined to the four spec cards (**Current experiment · Engine status ·
  Missing details · Recommended next action**) + new prompt chips; technical content stays under collapsed
  expanders. **Agent behaviour for the four example prompts** (message/routing data only — no scientific
  calc): `domains._THERMAL_RE` improved so "heat … at NNN °C / phase change(s)" classifies as
  **thermal_treatment** (was mis-routing to `red_mud_upcycling`); the mechanical/battery `classify` branches
  gained the `and not leaching` guard (a leaching prompt mentioning "cathode"/"ionic strength" stays
  leaching → PHREEQC offered); `PLANNING_DOMAIN_INFO` for polymer/thermal enriched (plastic type/form/size,
  binder, water/binder, curing, geometry, test standard; ramp rate, dwell, XRD/FTIR/TGA); the leaching
  ASK_USER lead now says **PHREEQC is available**; red-mud-upcycling's future-engine string no longer leads
  with PHREEQC. Result: leaching + red-mud-acid → PHREEQC offered; polymer-strength + thermal-calcination →
  planning-only, no PHREEQC; thermal classified correctly. **`EXECUTABLE_DOMAINS` unchanged**
  (`{leaching_geochemistry → phreeqc}`); the policy gate still blocks PHREEQC for every planning-only domain.
  Verified by a 4-lens adversarial review (no-science-change / nothing-removed / domain-routing-correct
  incl. 10+ leaching+temperature edge cases / dark-advanced-identity — **all pass, 0 real bugs**);
  `tests/test_app_tabs_smoke.py` rewritten for the 7-section nav, `tests/test_ui_modularization.py` updated
  (10 render modules: + results / engine_library / settings, − engine_settings), `tests/test_agent.py` gained
  the four-prompt routing + leaching-cue-wins + domain-specific-planning-vars tests; three pre-existing
  AppTest tests (`test_target_matching` / `test_material_profile` / `test_batch_executor`) gained a one-line
  `nav_section="Workspace"` step (Simulate now lives in Workspace, not a default tab). README + docs updated
  (dark seven-section nav, `docs/ai_architecture.md` future plugin-engine roadmap).

- **Robust natural-language understanding for the agent** (NLU layer; AI-first with a deterministic fallback;
  **no scientific / PHREEQC / execution / source-term / database / ranking / validation logic change** — the agent
  stays off the result path). The assistant no longer needs exact wording — it copes with messy, informal,
  misspelled, incomplete prompts like a chat assistant. New **`flyash_phreeqc_ml/agent/nlu_extractor.py`** is the
  understanding layer (it mirrors `ai/scenario_parser`, agent-state-aware): **one** grounded LLM call returns a
  structured `understanding` block **and** the proposed action (so the orchestrator makes a single call), then
  deterministic code **validates + normalizes** it before anything is applied; with no key a robust rule-based
  parse runs (flagged "more limited without AI"). It owns: `normalize_text` (typo/informal-unit/spacing fixes —
  `.5m`→`0.5 M`, `na oh`/`sodum hydroxde`→`NaOH`, `fli ash`/`CFA`→`Class C fly ash`, `redmud`→`red mud`,
  `1 hr`→`60 min`, leading-dot decimals, molar-`m`→`M`), canonicalization (`canonical_leachant` — careful: a bare
  "acid" is **ambiguous**, never silently HCl; permissive case-insensitive element extraction), schema-repair +
  numeric **validation** (`validate_value` / `_negative_fields` — negatives + out-of-range temperature/molarity
  **rejected** and turned into a question, never applied), assumption flagging (an assumed value like "room temp"
  → 25 °C is folded in **and** marked `needs_confirmation`), change/conflict detection (`compute_changes`), and
  the **"I understood this as…"** card (`build_understanding_card`). **Hard rules:** it extracts **only** the
  experiment set-up (material / leachant / masses / volume / time / temp / CO₂ cover / target elements / desired
  outputs) and **never** a composition, release fraction, measured value, computed pH/result, or validation status
  (those are stripped defensively; the existing `FORBIDDEN_ARGUMENT_KEYS` still strips the action args). The merge
  moved into a pure **`agent_state.apply_delta`** (scalars overwrite so corrections win; lists union — or replace
  for the corrector; assumptions flagged/cleared); `agent_state` stays AI-free (`merge_user_message` is now a thin
  wrapper). The orchestrator calls `nlu_extractor.extract` (replacing the old `_ai_action`), classifies the domain
  on **normalized** text, and surfaces a **"what changed"** note, a **clarify** note (rejected/ambiguous), and a
  one-time **limited-without-AI** note; a new **`apply_correction`** powers the UI's inline editor. `agent_prompts`
  gained the `understanding` schema + messy-text rules; `agent_policy._missing_question` now asks the **1–3** most
  important fields (errors first, capped); `safety.scientific_warnings` adds an honest **Sc/REE trace-element**
  caveat. `ui/assistant_tab.py` renders the understanding card + **"✏️ Edit what I understood / ✋ That's not
  right"** inline corrector (works AI-on or AI-off; no advanced-tab hunting). **`nlu_extractor` is the second
  AI-touching agent module** (with the orchestrator) — both still import no executor and no result path (pinned by
  `tests/test_ai_boundary.py`). Covered by `tests/test_nlu_extractor.py` (normalization, careful-acid, impossible-
  value rejection, the three messy example prompts, AI `understanding` validation + forbidden-key stripping,
  invalid-JSON safe fallback, change detection, JSON-safe card) + new `tests/test_agent.py` cases (messy prompts
  via rules **and** AI, follow-up merge, correction announced, inline-corrector edit, "run everything
  automatically" never auto-executes, ambiguous-acid asked, limited-note-once, no-raw-text-in-card). Docs
  `docs/assistant_agent.md`; README updated.

- **Agent Council v1 — an advisory review layer** (council adds a five-role review; **no scientific / PHREEQC
  input / execution / source-term / database / ranking / validation logic change** — the council is advisory,
  off the result path, runs nothing, decides nothing). New **`flyash_phreeqc_ml/agent/agent_council.py`** makes
  the assistant feel like a *team of research advisors*: after the orchestrator understands a message and chooses
  an action, `run_council` produces five `RoleAssessment`s (**Experiment Understanding · Domain & Engine Router ·
  Scientific Critic · Experiment Design Advisor · Results & Validation Critic** — each `short_assessment` /
  `concerns` / `missing_information` / `recommended_next_action` / `confidence` / `blocking_issues`) + one
  `CouncilReview` synthesis (`understood_scenario` / `likely_domain` / `executable_engine_status` /
  `planning_or_execution_status` / `key_missing_details` / `assumptions_to_confirm` / `scientific_warnings` /
  `recommended_next_user_question` / `safe_next_action`). **Safety by construction:** it imports **no executor
  and no tool registry** (it can't run anything), it **never decides the action** (`safe_next_action` merely
  *mirrors* the orchestrator's choice; `respond(council=True)` runs it **after** the action is chosen and never
  changes it), and the **canonical synthesis fields** (engine status, `scientific_warnings`, missing details,
  safe action) are **code-generated** from the existing domain/safety/missing-field validators — the AI council
  (one grounded call) enriches only the **role prose + understood-scenario + the single next question**, merged
  onto the deterministic baseline so it can **never weaken a caveat**. **AI off / failed → deterministic** review
  (`DETERMINISTIC_NOTE`). It **rejects unsafe asks** ("run everything automatically", "assume data", "validate
  my result" → `UNSAFE_REJECTION` + a Validation-Critic blocking issue). Provenance stores only the **derived**
  `to_safe_dict()` (no raw model text); `agent_state.last_council` is duck-typed `object` so `agent_state` stays
  AI-free. **UI:** a **Council Review** card in `ui/assistant_tab.py` (synthesis up top; the five roles under
  *"Show council reasoning"*; no raw JSON; a consent-gated toggle). **Shipped alongside, five live-eval robustness
  fixes** (no scientific calc change): **(A)** thermal-pretreatment-then-leach keeps the calcination temperature
  out of the leach `temperature_C` (`nlu_extractor.is_thermal_pretreatment_then_leach` / `pretreatment_temperature`,
  150 °C threshold) and asks for the leach temp/solution; **(B)** `domains.classify` routes a binder/geopolymer
  **strength** study whose only aqueous cue is a bare pH to **cementitious_binder** (planning-only), not executable
  leaching (`_STRONG_AQUEOUS_RE`); **(C)** out-of-scope elements (Ni/Co/Mn/…) are captured as
  `unsupported_elements` (`nlu_extractor.detect_unsupported_elements`, cluster heuristic) + warned, never dropped;
  **(D)** a bare "fly ash" stays generic (class unknown), never silently Class C (prompt + the council asks C vs
  F); **(E)** the clarifying questions are capped at ≤3 (prompt + `_first_question`). `agent_prompts` gained the
  `understanding` fields `pretreatment_temperature_C` / `unsupported_elements` + the D/E/A/B/C rules. The third
  AI-touching agent module (with orchestrator + nlu_extractor); all three import no executor / no result path
  (pinned by `tests/test_ai_boundary.py`). Covered by `tests/test_agent_council.py` (five roles, synthesis,
  no-raw-response, leaching/plastic/thermal-leach/geopolymer/unsupported/safety/question-cap, AI-merge-keeps-
  canonical) + `tests/test_nlu_extractor.py` (A/C/D). Docs `docs/assistant_agent.md`; README updated.

- **Literature Research Agent v1 — search reliable APIs + curate an evidence library** (a new
  `flyash_phreeqc_ml/literature/` package + an **Evidence Library** UI section; **off the scientific
  result path** — extracted evidence is a *future* training dataset, never a measured/validated value;
  **no model trained, no strength predicted, no Google Scholar scraping, no raw LLM responses stored**).
  For a domain with no validated engine yet, the assistant now says plainly it **cannot run a validated
  model yet** and offers to **search literature + build an evidence dataset** (`domains.planning_only_message`
  enriched; pinned by `tests/test_literature_agent.py`). Package modules: `source_schema.py`
  (supported sources + the provenance-carrying `PaperCandidate`; **`GOOGLE_SCHOLAR_SUPPORTED=False`** +
  `ensure_supported_source` rejects it — there is **no scraper**, pinned by a no-scraper-import test),
  `search_clients.py` (OpenAlex / Crossref / Semantic Scholar / PubMed clients behind one **mockable**
  `_http_get_json`; all **keyless**, optional polite-pool email / API key from **env only**; multi-source
  search + DOI/title de-dup; a **manual** entry path — no scraping; graceful empty result on no network),
  `ranking.py` (transparent **deterministic** relevance ranking + a "why useful" explanation + an
  *extractable-data* flag), `evidence_schema.py` (`LeachingEvidence` + `CompositeEvidence` — **provenance
  required**, missing → **null**, banded **confidence** + an **extraction scope**; an **abstract-only**
  extraction is **capped at medium**), `extraction.py` (the **only** AI module — extracts values from a
  paper's abstract; **missing → null**, **never fabricates**, conflicts flagged, no hidden inference,
  AI-off → empty row with an honest status, and the **raw model response is never stored**; mirrors
  `ai/scenario_parser`), `evidence_store.py` (per-run JSONL store — **provenance enforced** on save, a
  **safe-path guard**, CSV export; stores structured values + citation only, **never abstracts/full text**;
  lives under the gitignored `experiments/<run>/outputs/literature/`), `research_agent.py` (query
  generation + the search orchestration → ranked, cited candidates). **UI:** `ui/evidence_library.py` +
  a new **Evidence Library** nav section (search box · domain/source selectors · ranked candidates with
  why/extractable · consent-gated AI extraction · evidence table with confidence/provenance · CSV export ·
  manual entry). **Boundary** (pinned by `tests/test_ai_boundary.py`): only `extraction` imports the AI
  client; no literature module imports an executor or the result path; the result path never imports the
  literature package. Covered by `tests/test_literature_agent.py` (17 tests) + boundary/modularization/
  smoke updates (854 passed / 3 skipped). Docs `docs/literature_agent.md`; README + `docs/assistant_agent.md`
  updated. (Distinct from the older `ai/literature.py`, which *proposes quarantined literature values* for
  the fly-ash closure arithmetic — this new package is a *scholarly-search + evidence-library* system.)

- **ML Surrogate Prediction Engine v1 — trained-model predictions for unsupported domains** (a new
  `flyash_phreeqc_ml/ml_models/` package + a **Prediction Models** UI section; **off the geochemical
  result path, no AI/LLM in the engine** — numbers come from scikit-learn, never a language model).
  The first *trained-model* prediction engine: it turns the **Evidence Library**'s curated, cited data
  into a fast screening estimate of **polymer-composite / fly-ash + plastic mechanical properties**
  (`compressive_strength_MPa` first, then flexural / density / water-absorption). Modules:
  `model_schema.py` (targets, model types, validation statuses, the joblib-serialisable `TrainedModel`
  container — **no sklearn import**, so schema/registry/UI import without scikit-learn),
  `feature_schema.py` (numeric + categorical features + a **core-feature** set), `training_data.py`
  (`TrainingRow` with provenance + a **`user_review_status`**; `from_composite_evidence` maps an
  Evidence-Library composite row → a **pending** training row; `eligible_rows` is the gate —
  **approved + provenant + (literature) confidence ≥ 0.45**, synthetic-demo **quarantined**; JSONL
  persistence; clearly-labelled synthetic `demo_rows`), `preprocessing.py` (rows → frame +
  `ColumnTransformer` median-impute / one-hot tolerant of unseen values; sklearn lazy),
  `uncertainty.py` (approximate interval — random-forest spread else CV residual σ), `train.py` (the
  **data-sufficiency gate** `MIN_REAL_TRAINING_ROWS=10` → typed `InsufficientTrainingDataError`;
  out-of-sample metrics via held-out split / k-fold CV; typed `SklearnNotAvailableError` with an
  install message; **never labels a model "validated"** — only `experimental` / `demo`),
  `predict.py` (value + interval + status + **applicability / out-of-domain / missing-feature /
  implausible-output** warnings; **refuses** on no-model / unsupported / incomplete inputs),
  `model_card.py` (the honest exportable card: intended / NOT-intended use, limitations, known
  failure cases, applicability domain, validation status, extraction-uncertainty warning),
  `model_registry.py` (save / load / list under a **safe-path-guarded**, gitignored
  `experiments/<run>/outputs/model_registry/` via `run_manager.model_registry_dir`; **overwrite guard**;
  cheap `has_strength_model` / `available_targets` index queries that never load an artifact). **Three
  honesty rules:** (1) only **approved** evidence/lab rows train a real model by default (AI-extracted
  rows arrive `pending`; low-confidence / provenance-less rows excluded unless the UI's *exploratory
  mode* opts them in); (2) a model is at most **experimental** (CV metrics on training rows), **never
  validated** (that needs measured experiments — still future); (3) a **demo** model trains on
  synthetic data, is loudly labelled, and is never mixed with real rows unless explicitly chosen.
  **UI:** `ui/prediction_models.py` (curate dataset from the Evidence Library → **review/approve** in a
  data-editor → train real or demo model → metrics + model card + export → predict form with
  uncertainty + warnings → saved-models registry). **Assistant integration** (message/routing only —
  the agent imports **nothing** from `ml_models`): `domains.supports_ml_surrogate` /
  `ml_surrogate_offer` / `ML_SURROGATE_MARKER` + `planning_only_message(domain, ml_model_available=…)`
  (default message byte-identical); `agent_orchestrator.respond(..., ml_model_available)` appends the
  surrogate offer once for a planning-only mechanical domain; the **UI computes availability** via a
  read-only registry query and passes the flag, so for a "predict compressive strength" prompt the
  assistant offers the **ML surrogate when a model exists, else literature/data-building — never a
  fabricated number, and PHREEQC is never the strength engine**. Boundaries pinned by
  `tests/test_ai_boundary.py` (no `ml_models` module imports an AI client / references a raw LLM
  response / API key; none imports an executor; the geochemical result path + the simulation layers
  never import `ml_models`); behaviour by `tests/test_ml_models.py` (schema/provenance, eligibility,
  gate, training metrics + card, registry safe-paths + overwrite guard, prediction
  value/interval/warnings/refusals, demo labelling, assistant routing). Requires scikit-learn (optional,
  lazy — graceful install message without it); trained models/datasets are gitignored run outputs. Docs
  `docs/ml_surrogate_engine.md`; README + `docs/assistant_agent.md` + `docs/literature_agent.md` updated.

- **End-to-end product acceptance pass** (verification only, no code change). The platform was run as a
  full acceptance test from a user's perspective and **passed**: `compileall` clean, `pytest` **890
  passed / 3 skipped**, the Streamlit app **boots (HTTP 200)**, and all six workflows behaved correctly
  and safely on the **deterministic (AI-off) path** — Assistant→PHREEQC (messy prompt fully parsed,
  temp 25 °C flagged assumed, council shown, asks for composition, run **parks** for confirmation,
  PHREEQC-unavailable fails gracefully as *simulation not validation*); plastic-strength routes
  planning-only (no PHREEQC, no fabricated MPa); Evidence Library uses official APIs only (no Scholar
  scraper), honest no-network path, provenance required, no raw response stored; ML demo labelled
  synthetic/not-validated with interval + OOD warning; unapproved rows excluded from real training;
  validation separation holds (`residual = measured − predicted`, no-mapping → NaN, `valid` only with
  exact mapping). **Live AI was not exercised in this run** (deterministic path tested to avoid sending
  data / storing responses; the live-AI NLU was validated separately in the earlier 20-prompt eval, and
  the full real-PHREEQC arc against 3.8.6 was verified earlier). **Known follow-up polish (UX only, not
  blocking):** the Assistant page is visually dense; the Council Review may want a collapsed / default-off
  mode; material-composition entry should be surfaced better (it's buried in an Advanced expander); the
  **Evidence Library → Prediction Models** (curate → approve → train) flow should be signposted in-line;
  and the PHREEQC setup/demo path should be made easier for a reviewer (no real run without a
  user-supplied CLI + CEMDATA18).

- **Settings live-AI enable toggle (persistent, capability-gated)** (UI/UX + AI-config plumbing;
  **no scientific / PHREEQC / result-path change**). Adds a clear **Settings → "Enable live AI
  assistant"** master switch so a user with a detected key can turn live AI on from the UI — the
  enable control previously **did not exist in Settings** (the only switch was a consent checkbox
  buried in the Assistant tab, and the "disabled" caption misreported a missing-SDK as "no AI key").
  `ai/config.live_ai_active(cfg, toggle_on)` is the pure, key-free gate (*capable* = key + SDK
  present, **AND** the toggle on); a stale toggle never overrides a lost key/SDK. Settings shows a
  4-card status panel (**API key · AI SDK · Live AI · model/provider**, all key-free) + the toggle
  (operable only when capable, with the missing-key / missing-SDK reason). The Assistant uses live AI
  **only** when the toggle is on, shows the per-turn outcome ("last response used live AI: yes/no") +
  a debug-safe booleans panel (never the key). **Persistence fix:** the choice is stored in a
  **plain** session key (`ui.state.LIVE_AI_KEY`), never the toggle's widget key — Streamlit's
  widget-state GC drops a widget-keyed value the moment the widget stops rendering (on navigation),
  which previously **reset AI to off after the first prompt submission**; the toggle now uses its own
  `LIVE_AI_WIDGET_KEY` and syncs into the plain key on change, so it survives navigation + reruns. An
  **AI call failure** falls back deterministically for that one turn with a visible warning
  (`agent_orchestrator.AI_FALLBACK_NOTE`; `agent_state.last_used_ai` / `last_ai_fell_back`) and
  **never disables the toggle** (only Settings can). Covered by `tests/test_ai_config.py`
  (`live_ai_active`) + `tests/test_ai_settings_ui.py` (AppTest: toggle visible/operable when key
  detected, disabled + explained without a key, **persists across navigation + submission**,
  `use_ai=True` only when on, AI-failure one-turn fallback keeps the toggle on, no key value
  rendered). The key is read only from env / `st.secrets` and is never shown or logged.

The app's current direction continues this generalization + presentation arc (generic
terminology, two non-mixed plot families, per-run results, canonical mapping statuses with
structured matched/missing/conflicting fields) — see **Direction: generalization + presentation**
below. The first ML scaffolding now exists as the **experimental PHREEQC surrogate** (Prompt 12), but
it is deliberately **isolated from every scientific result path** — a fast approximation in the
Audit/Help tab only, never a measurement, never feeding comparison/residual/mapping. The first step
toward the *correction* layer exists as **descriptive systematic-bias estimates** (Prompt 13) — plain
statistics over exact-mapped residuals, explicitly **not** a trained model — and the first *learned*
correction now exists as the **experimental GP residual-correction model** (Prompt 14), but it is
**hard-gated** (≥30 exact pairs / ≥3 conditions), **LOCO-validated against the constant-bias
baseline**, and **display-only** (a raw-vs-corrected overlay that never replaces PHREEQC output or
feeds mapping/validity/the comparison CSV). A correction trusted enough to drive results is still
future work — it stays an overlay until LOCO shows it beats the bias bands on unseen conditions.

> **Two different `experiments/`.** `flyash_phreeqc_ml/experiments/` is the *Python package*
> (planning + QA/QC). The repo-root `experiments/` is the *data folder* of run save-files
> (gitignored except its `README.md`). Don't confuse them.

## Direction: generalization + presentation (current phase)

The app is being steered toward a **generic measured-data → model-prediction → mapping →
residuals → validation-status** workflow, with PHREEQC and fly ash as the *current
implementation*, not a hard limit. Follow these rules when writing new code/UI:

- **Generic terminology going forward.** New UI strings and new code use generic terms: *measured
  data*, *measured record/group*, *model prediction*, *simulation output*, *mapping status*,
  *residual*, *validation status*. PHREEQC-specific wording stays in the parsers, model-specific
  modules, and advanced/metadata expanders. Don't mass-rename existing code yet — but don't add new
  fly-ash/PHREEQC wording to generic workflow code either.
- **Cup-cover condition semantics (fly ash dataset).** Condition codes are CO₂-exposure cup covers:
  **OA = open air** (direct atmospheric CO₂ exposure), **PF = plastic flap cover**, **GS = glass
  cover**. PF and GS likely reduce CO₂ exchange but must **never** be described as "sealed" unless
  airtight sealing is experimentally confirmed. This is **dataset-specific metadata**, not universal
  app logic.
- **Two plot families, never mixed.** (a) *Measured-data overview* — all measured rows for a
  variable, labeled "Measured data only — no model comparison", renders even with zero model output.
  (b) *Model comparison* — only rows with a measured value **+** a saved mapping **+** a model
  prediction; always shown with row counts and an **excluded-rows table with per-row reasons**.
- **Per-run results.** Comparison outputs and figures belong to a run (`experiments/<run>/outputs/`),
  stamped with provenance. The Results tab must **never** display a comparison generated from a
  different run's data.
- **Residual convention everywhere:** `residual = measured − model predicted`. Positive = measured
  higher than model. Near-zero = better agreement **only if the mapping is scientifically valid**.
- **Mapping statuses are canonical:** `exact`, `scenario-level only`, `unsafe`, `needs new
  simulation` (generic name; the UI may append the model name from context). Every suggestion must
  expose **structured matched / missing / conflicting fields**, not just a prose reason.

## Working rules (project-specific)

- **No ML on the result path.** Do not wire any model into comparison/residual/mapping/validity.
  The experimental ML that exists (surrogate Prompt 12, bias stats Prompt 13, GP residual correction
  Prompt 14, GP model-incompleteness estimator Prompt 27) is deliberately isolated: the surrogate is
  Audit/Help-only; the residual correction is **hard-gated** (≥30 exact pairs / ≥3 conditions, enforced
  by a typed error — never lower it) and **display-only**; the incompleteness model is **hard-gated** the
  same way (≥30 well-determined rows / ≥3 conditions), additionally **refuses to fit noise** (reduced-χ²
  guard) and **never trains on literature-provenance rows**, and its output is a labelled "predicted
  shortfall" estimate that never enters closure arithmetic. Real measured release data still does not exist
  in `data/raw/experimental_icp/` (only the blank template), so for fly ash these models have **no data to
  train on yet** and Phase 2 comparison remains the scientific ceiling — the gate is what keeps that honest.
  The **ML Surrogate Engine** (`ml_models/`, Prediction Models tab) is a *separate* prediction engine for
  **mechanical/composite properties** (a different domain) — it is **off the geochemical result path** too
  (it never touches comparison/residual/mapping/validity), gated on **approved** evidence/lab rows, at most
  *experimental* (never "validated"), and its number comes from scikit-learn, never the LLM. Do not wire it
  into the geochemical comparison either.
- **Generated artifacts are not committed** unless explicitly requested. `data/processed/*.csv`,
  `reports/figures/*.png`, `outputs/tables/*.csv`, and the generated run sheet
  `data/raw/experimental_icp/experiment_plan.csv` are gitignored and re-creatable by
  running the scripts.
- **Confidential raw research data:** do not commit raw research data unless the user confirms
  it is allowed. `data/raw/` is currently tracked, so be deliberate about anything added there.
  The remote is confirmed **private**, and the existing `data/raw/` contents (UMass mix-design
  workbook, PHREEQC files) are approved to push there; re-confirm if the remote changes or any
  *new* raw dataset is added.
- **The CFA+MK mix-design workbook lives in `data/raw/icp_mix_design/`** (config `ICP_DIR`). It was
  renamed from the fragile space-named `experimental icp/` folder — which differed from the Phase-2
  `experimental_icp/` (`EXPERIMENTAL_ICP_DIR`) only by a space. They are **two distinct folders for
  different data** (mix-design inputs vs. measured release); do not merge them (`scripts/02_parse_icp`
  globs `*.csv` in `ICP_DIR`, so the release CSVs must stay out of it). `.gitignore` ignores any
  `*.xlsx/*.xls/*.csv` under `icp_mix_design/` with a `!`-re-include keeping **only** the one approved
  UMass workbook tracked — so a *new* ICP file is never committed accidentally.
- **Pre-commit data-safety hook.** `flyash-phreeqc-ml/scripts/hooks/pre-commit` (tracked) blocks
  staged `*.xlsx`/`*.xls`, anything under `data/raw/experimental*`, `data/processed/`,
  `experiments/*/outputs/`, and `*release*`/`*measured*` CSVs outside `tests/fixtures/synthetic/`
  (the one approved UMass workbook is allowlisted). **Install it** (from the parent `CursorCode1`
  repo root, since this is a subdir): `cp flyash-phreeqc-ml/scripts/hooks/pre-commit
  .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`. Bypass a deliberate, reviewed exception
  with `git commit --no-verify`. (The hook is not auto-installed — `.git/hooks/` is per-clone.)
- **Measured release CSVs are gitignored by default.** `.gitignore` ignores `*release*.csv`,
  `20*_release*.csv`, `*measured*.csv`, the manual-entry file, the generated plan
  (`experiment_plan.csv`), and the generated `sample_phreeqc_map.csv` in
  `data/raw/experimental_icp/`, with a `!`-re-include keeping **only** the blank
  `experimental_release_template.csv` tracked. So real lab data stays out of git unless
  deliberately force-added. (gitignore comments must be on their own line — an inline `#` becomes
  part of the pattern.)
- **Experiment-run save-files are gitignored by default.** `.gitignore` ignores
  `experiments/*/data/*.csv`, `experiments/*/outputs/`, and `experiments/*/run_config.yaml`;
  **only** `experiments/README.md` is tracked. Run data (lab, literature, synthetic) and
  generated outputs stay out of git unless explicitly approved.
- **Run `pytest` before committing** any code change, and keep code modular, simple, and tested.
- **End-to-end workflow lock.** `tests/test_e2e_workflow.py` drives the full pipeline through
  `run_manager` directly (no Streamlit): create run → save synthetic measured data (4 conditions
  covering all four mapping statuses) → suggestion table → accept rules (bulk-exact + selected
  scenario-level; unsafe refused) → expand condition mapping → per-run comparison (the Prompt-1
  `comparison_path` + `write_comparison_meta` path) → `comparison_is_current` → `comparison_inclusion`
  (counts / exclusion reasons / residual signs / collapse / validity=preliminary) → mutate the data
  CSV and assert freshness flips; plus an alternate-profile unit pass. Keep it green and fast (no
  network; synthetic phreeqc_results frame) — it is the regression guard against silent pipeline
  breakage.

### Git layout (important)

This project is a **subdirectory inside a larger git repo** rooted at the parent directory
(`CursorCode1`), not its own repo. So `git status` run from here shows the parent repo, and
commits/pushes target it — stage paths as `flyash-phreeqc-ml/...`. Do **not** `git init` here
(it would create a confusing nested repo). When committing, prefer staging explicit files over a
blanket `git add` so untracked stray files (e.g. someone's half-named template copy) don't slip in.

## Commands

```bash
# setup (virtualenv lives at .venv/)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # runtime: pandas/numpy/matplotlib/openpyxl/streamlit
                                       #   + OPTIONAL anthropic (AI assist), scikit-learn/scipy (surrogate)
pip install -r requirements-dev.txt    # pytest

# main pipelines
python scripts/run_phase1.py            # Phase 1: parse -> processed CSVs -> master_dataset -> plots
python scripts/05_compare_experimental.py  # Phase 2: measured vs PHREEQC (no-op until data exists)

# experiment planning + QA/QC (pre-data; no ML)
python scripts/06_generate_experiment_plan.py    # -> data/raw/experimental_icp/experiment_plan.csv
python scripts/07_validate_experimental_data.py  # -> outputs/tables/experimental_validation_report.csv
python scripts/08_sustainability_score.py        # -> outputs/tables/sustainability_score.csv

# on-demand PHREEQC + surrogate (need a user-supplied PHREEQC CLI + CEMDATA18 database):
export PHREEQC_EXE=phreeqc                        # the PHREEQC binary (or put it on PATH)
export PHREEQC_DATABASE=/path/to/CEMDATA18-...dat # not redistributable — user-supplied
python scripts/09_generate_simulations.py --run "<run>"               # generate/run/ingest needs-new conditions
python scripts/10_sample_design.py --run "<run>" --n-samples 200      # LHS design -> surrogate_dataset.csv
# Both write their inspectable output (plan/design) even when PHREEQC is unconfigured, then stop cleanly.

# Optional AI (import-assist, grounded assistant, literature, NL Simulate planner) — off by default,
#   suggestion-only, never on the result path:
#   export ANTHROPIC_API_KEY=...        # or a Streamlit secret of the same name
#   export ANTHROPIC_MODEL=...          # optional model override (e.g. a cheaper model)
#   The sidebar "🤖 AI settings" panel shows status (enabled, provider, model, key detected — never the key).

# GUI (optional): thin Streamlit wrapper over the scripts above
streamlit run app.py

# tests
python -m pytest                        # full suite
python -m pytest tests/test_comparison.py::test_residuals_match_spec   # a single test
```

Scripts self-bootstrap `sys.path` via `scripts/_path_setup.py` / `conftest.py`, so they run
without `pip install -e .` (which is optional). If `pytest` isn't found, use `python -m pytest`
inside the venv.

## Architecture

The package `flyash_phreeqc_ml/` is split by concern; `scripts/` are thin entry points that wire
modules together and own all file I/O paths.

- **`config.py` is the single source of truth.** All filesystem paths, the experimental-release
  CSV **schema** (`EXPERIMENTAL_RELEASE_COLUMNS` / `EXPERIMENTAL_NUMERIC_COLUMNS`), key elements/
  phases, and the molality→mM factor live here. The shipped template CSV, the parser, and the
  tests all derive from this list — change the schema here, not in three places.
  (`PHREEQC_MOLALITY_TO_MM` is re-exported from `units.py`, the conversion authority.)

- **`units.py` is the single conversion authority.** Every unit conversion in the app routes through
  `units.convert(value, from_unit, to_unit, element) -> ConversionResult` (value + registry `id` +
  molar mass used + formula). It owns `MOLAR_MASSES` (IUPAC atomic weights, surfaced in the UI) and
  the `CONVERSIONS` registry (mg/L·ppm·ppb→mM, molality→mM, identity). **No silent fallbacks**:
  unknown unit/element raise typed `UnknownUnitError`/`UnknownElementError`. `config`, `calculations`,
  and `import_mapping` all call through it (one code path). It also defines the conversion-provenance
  companion suffixes (`_orig_value`/`_orig_unit`/`_conversion_id`). Imports nothing from the package
  (so `config` can re-export the molality factor without a cycle). Covered by `tests/test_units.py`.

- **`audit.py` is the append-only event log.** `log_event` / `read_audit` + typed convenience loggers
  write/read `experiments/<run>/outputs/audit_log.jsonl` (one JSON line per action, with
  `app_version`). No edit/delete API; logs ids/counts/statuses/hashes only (never values/contents);
  every call is defensive (warns, never raises). Instrumentation lives at the seams (`run_manager`
  data ops via lazy import, `scripts/05`, and app orchestration). The JSONL is gitignored under
  `experiments/*/outputs/`. Covered by `tests/test_audit.py`. (It also carries the Prompt-26
  `literature_proposed` / `literature_confirmed` events — the latter keeps the DOI/link + title + year so
  a confirmed literature value's downstream influence is traceable to the exact paper.)

- **`ai/literature.py` retrieves sourced literature values — quarantined by construction** (Prompt 26;
  opt-in, suggestion-only). `propose_literature_values(query)` uses the Anthropic **web_search** tool to
  propose `LiteratureCandidate`s, each **dropped in code** unless it has a supporting quote + a resolvable
  DOI/URL (DOI → `https://doi.org/…`; quote ≤25 words). Candidates land `literature-proposed` /
  `confirmed=False` in a **separate** per-run store `experiments/<run>/outputs/literature_values.jsonl`
  (never measured data / manifest / comparison). The **only** path into a calculation is
  `row_with_confirmed_assays` (confirmed-only, blank cells only, returns a source badge) — `mass_balance` /
  `attribution` never import it, so unconfirmed values are inert. `confirm_value(..., acknowledge_mismatch)`
  flips to `literature-confirmed` (citation retained, audit-logged) and **refuses** a conditions-mismatched
  value without the second acknowledgement. Reuses `import_assist`'s lazy client (disabled without a key).
  Covered by `tests/test_literature.py`.

- **`ai/config.py` + `ai/client.py` — the AI config/client layer** (foundation; opt-in, off the result
  path). `config.py` is the **single AI-config authority**: provider/model/key detection; an `AIConfig`
  snapshot that holds **no key** (only presence + source); precedence env-key (`ANTHROPIC_API_KEY`) >
  `st.secrets`, and model `arg > UI override > ANTHROPIC_MODEL env > secret > DEFAULT_MODEL`. Pure +
  import-safe (lazy `streamlit`/`anthropic`; `st.secrets` read only under a live Streamlit runtime).
  `client.py` builds an Anthropic client only when enabled and returns a structured, **key-free**
  `ClientResult` (never raises, never exposes the key). `import_assist` / `assistant` / `literature` /
  `scenario_parser` all resolve their key/model/client through this **one** layer; surfaced by the sidebar
  **🤖 AI settings** panel (no key entry in the UI — env/secrets only). Covered by `tests/test_ai_config.py`
  + `tests/test_ai_client.py`; the AI-off-the-result-path boundary by `tests/test_ai_boundary.py`. Docs:
  `docs/ai_configuration.md`.

- **`simulation/` (Simulate planning + gated execution) + `ai/scenario_parser.py` + `materials/`** — the
  forward-looking core. **Planning is off every result path; execution is gated + off every result path.**
  *Planning:* `scenario_schema.py` (scenario dataclasses + flat serialization + the plan-only caveat constants
  `PLAN_ONLY_LABEL`/`NON_PREDICTION_NOTE`; the machine status value is `matrix.STATUS_PLAN_ONLY="plan_only"`),
  `safety.py` (**deterministic** missing-field + scientific-warning analysis — caveats from code, not the AI),
  `rule_parser.py` (non-AI regex extractor), `matrix.py` (`build_simulation_matrix(scenario, ranges=…)` → a
  `status='plan_only'` plan table), and `phreeqc_input_builder.py` (**deterministic** draft `.pqi` from a
  confirmed scenario — water/NaOH/HCl templates + an optional material source term + only-available phases;
  never runs PHREEQC, writes no files, never invents composition; Steps 7–8). `ai/scenario_parser.py` extracts a
  scenario via the key-safe AI client (strict-JSON; invalid → controlled error) and **falls back to
  `rule_parser`** when AI is off; **AI never writes PHREEQC input**. *Material side:* `materials/` (composition
  manager — provide/review/**confirm** a profile; only a confirmed `usable_assay` feeds the preview; Step 7) and
  `source_terms.py` (release model → REACTION-block source term; release fractions are **user assumptions**;
  Step 7b). *Database side:* `database_compatibility.py` + `phase_templates.py` (only **available** phases added;
  Step 7c). *Execution (gated, explicit click only):* `phreeqc_executor.py` (`check_availability` /
  `execute_preview` / `parse_outputs`; runs the user-supplied CLI into the safe `outputs/simulations/`
  workspace), `batch_executor.py` (small **capped** sweep + plot-ready frames; Step 9). *Reasoning over executed
  outputs:* `strategy.py` (ranking + refined sweep) and `target_matching.py` (inverse search; Step 10) — both
  **rank/score model predictions, never validate**. *Provenance:* `run_registry.py` (saves a run +
  `refinement`/`target_match` blocks to `outputs/simulation_runs/`). **Nothing here runs automatically, none
  overwrites data or becomes verified data, and none imports a comparison/residual/mapping module** — every
  boundary (no AI on the result path, no executor in the pure modules, science never on the UI path) is pinned by
  `tests/test_ai_boundary.py`. Each module has its own completed-phase bullet above; per-module docs live in
  `docs/simulation_planner.md` · `material_profiles.md` · `material_release.md` · `database_compatibility.md` ·
  `phreeqc_execution.md` · `simulation_strategy.md` · `target_matching.md` · `simulation_runs.md`, covered by the
  matching `tests/test_*` files. **Real PHREEQC is not installed by default**, so the run paths are gated +
  mock-tested (with one optional integration test each); the arc was verified end-to-end against a from-source
  PHREEQC 3.8.6 build.

- **`report.py` builds the offline review bundle.** `build_report(run_name)` composes the existing
  layers (provenance, inclusion, traces, bias, **element recovery**, conversions, audit) into a
  self-contained `validation_report_<ts>/` (report.html + CSVs + figures + MANIFEST.json with SHA-256).
  Pure composition — it adds no chemistry; the Prompt-4 validity rules drive all honesty wording. The
  **Element recovery** section (Prompt 25, `_recovery_records` / `_recovery_table` / `_recovery_summary`)
  integrates measured closure (Prompt 22) + PHREEQC attribution (Prompt 24; *unavailable* offline → the
  whole gap is unexplained unless a parsed selected output is supplied) + **confirmed** literature
  starting-assay stand-ins (Prompt 26, provenance-flagged with the DOI/link), emitting per-element-per-
  condition terms + a generated narrative + an `element_recovery.csv` and a summary sorted by unexplained
  fraction; `MANIFEST.json` gains `recovery_classification` tagging each term measured / derived / modeled
  / literature-confirmed. Gitignored run output. Covered by `tests/test_report.py`.

- **`profiles.py`** — the **generalization layer** (additive; pure, no chemistry/ML). Two frozen
  dataclasses describe a dataset + model so the same code can serve more than fly ash + PHREEQC
  **without renaming the package**: `DatasetProfile` (id/time/replicate columns, condition column +
  code dict, variable columns + units, overview variables, `important_fields` + `tolerances` for
  grouping/mapping, `comparison_variable_spec`, and a `grouping` flag) and `ModelProfile` (model
  `name` used in UI strings, prediction metadata fields, parser entry point). `FLY_ASH_PROFILE` /
  `PHREEQC_PROFILE` are populated **by reference** from `config.py` (still the single source of truth)
  — incl. the OA/PF/GS cover dict from `config.CONDITION_CODE_DESCRIPTIONS`. Profiles are threaded
  through the existing seams with a **fly-ash default**, so all current behaviour is unchanged:
  `replicates.condition_key`/`annotate`/`replicate_summary`/`mapping_status` (fly-ash bespoke key when
  `grouping=="fly_ash"`, else a generic `important_fields` key), `scenarios.sample_condition_code` /
  `_metadata_alignment` / `score_scenario` / `suggest_mappings` (condition vocab from the profile),
  `mapping_table.build_suggestion_table`/`condition_candidates`, `compare.inclusion.comparison_inclusion`
  (variable spec from `profile.comparison_variable_spec`; `inclusion.VARIABLE_SPEC` now references the
  profile), and `viz/measured_overview` (overview variables + time column from the profile). A second
  synthetic profile drives the whole chain in `tests/test_profiles.py`. **Material side (Prompt 28):**
  a `MaterialProfile` (frozen) bundles the *material/reagent/phase* specifics — `material_id` /
  `display_name`, `relevant_elements`, `mass_balance_elements`, `candidate_phases` (phase→element),
  `precipitate_in_measured_solid`, `default_reagents`, and a provenance-flagged `declared_assay`
  (`AssayValue` with `measured` / `literature-confirmed` / `literature-proposed`; a *proposed* assay is
  **quarantined** — `is_usable` False — until a human confirms it). `DatasetProfile` gained an additive
  `material` field; module-level **resolvers** (`profiles.mass_balance_elements` / `candidate_phases` /
  `precipitate_in_measured_solid` / `default_reagents` / `usable_declared_assay`) read the material first
  and fall back to the DatasetProfile's own batch fields (so material-less / legacy profiles are
  unchanged). `mass_balance`, `attribution`, the `report` recovery section, and `incompleteness_model` all
  call these resolvers — **none hard-codes a fly-ash element or phase** (guarded by
  `tests/test_material_profile_agnostic.py`). `FLY_ASH_MATERIAL` (Ca/Si/Al/Fe/Na/K, NaOH; closure still
  OFF) and a second-material stub `RED_MUD_MATERIAL` / `RED_MUD_PROFILE` (Ti/V/Fe/Al + REE, anatase/
  hematite/… phases, the **opposite** filtration flag) prove the abstraction. A **no-code profile path**
  (`dataset_profile_from_spec` / `material_profile_from_dict` / `load_dataset_profile`, JSON always + YAML
  if PyYAML is present) lets a researcher define a new material in one file
  (`docs/defining_a_material.md`, example `docs/examples/red_mud_material.json`); a literature-proposed
  declared assay is rejected from any calculation until confirmed. Ti/V molar masses were added to
  `units.MOLAR_MASSES`. **Seams not yet threaded** (noted for a future prompt): `overall_mapping_status`
  / `conditions_needing_simulation` / `condition_mean_comparison` and the per-sample `id_column` still
  assume the fly-ash default; the `mapping_status` acid/CO₂ conflict checks are fly-ash-specific (they
  simply no-op when those columns are absent); and the on-demand PHREEQC `.pqi` generation still uses the
  fly-ash NaOH/CO₂-cover templating (the measured closure / attribution arithmetic / recovery / training
  frame are fully material-driven).

- **`parsers/`** turn raw files into tidy DataFrames:
  - `pqo_parser.py` is the core. PHREEQC `.pqo` output is verbose text; the parser walks it
    line-by-line tracking `(simulation, state, solution)` context, where `state` is `initial`
    (pre-reaction) or `batch` (post-equilibration). It parses four dashed-banner blocks —
    *Solution composition, Description of solution, Phase assemblage, Saturation indices* — into
    one wide `results` row per state plus long `saturation`/`assemblage` tables.
  - `pqi_parser.py` reads input solutions + equilibrium phases; `selected_output_parser.py`
    handles the cleaner `SELECTED_OUTPUT` tables when present.
  - `icp_parser.py` does double duty: best-effort extraction from the messy CFA+MK mix-design
    workbook, **and** the Phase-2 experimental ingestion (`parse_experimental_release`,
    `load_experimental_release`, `has_measured_data`).

- **`datasets/build_master.py`** joins each PHREEQC output state to its input composition on
  `solution_number` (output `.pqo` and input `.pqi` filenames differ, so the join is by number,
  first definition wins) → `master_dataset.csv`.

- **`compare/residuals.py`** (Phase 2) converts PHREEQC molality to mM and computes
  `residual_<X> = measured − PHREEQC`. Measured samples link to PHREEQC runs via an explicit
  `sample_id → record_key` mapping (`data/raw/experimental_icp/sample_phreeqc_map.csv`); with no
  mapping, predictions/residuals stay NaN rather than mis-joining (a deliberate, visible state).

- **`compare/inclusion.py`** — the **single** comparison-inclusion function (pure, no Streamlit).
  `comparison_inclusion(data, mapping, comparison_df, variable, *, manifest, include_unsafe,
  min_valid_rows)` classifies every comparison row, for the selected `variable` (`VARIABLE_SPEC`:
  final_pH/Ca/Si/Al/Fe), as **plotted** or **excluded with exactly one reason** in priority order
  (`no saved mapping` → `mapping is unsafe (excluded by default)` → `model prediction missing this
  variable` → `measured value missing/non-numeric`), so plotted ∪ excluded partitions the rows and
  the counts always add up. It joins the four `replicates.mapping_status` values onto rows, plots
  only `exact`/`scenario-level` by default (unsafe only when `include_unsafe`, then `flagged`),
  flags the **scenario-level collapse** (unique predictions / plotted ≤ 0.5 or any prediction reused
  ≥ 3×), and picks **one** overall `validity` (`valid` / `preliminary` / `single-sample` / `unsafe`
  / `needs new simulations` / `nothing to compare`) — only `valid` implies the model was validated.
  Rules documented in `docs/comparison_inclusion.md`. The plots **consume this output, never
  re-derive filters.** Covered by `tests/test_inclusion.py`.

- **`viz/`** — `plots.py` (Phase 1 exploratory) and `compare_plots.py` (Phase 2, the
  *model-comparison* plot family — only emits figures when measured/PHREEQC pairs exist;
  `make_comparison_plots(..., statuses=None)` styles scatter points by mapping status with a legend
  when a `sample_id → status` map is given, default `None` = unchanged so the `scripts/05` CLI is
  untouched; `comparison_scatter_figure(plotted, variable)` returns a live status-styled
  measured-vs-model figure for the app, consuming `inclusion["plotted"]`), plus
  `measured_overview.py` — the **measured-data-only** plot family (pure, no Streamlit/matplotlib).
  `available_variables(data)` lists `final_pH` + ICP columns that actually carry numeric data (never
  empty columns); `prepare_overview(data, variable)` returns a tidy plot frame (`sample_id,
  condition_key, replicate_id, value`, + `time_min` when a numeric time exists), an `excluded` table
  (blank / non-numeric values, with reasons — counts add up: `n_shown + n_excluded == rows`), and
  per-condition `group_stats` (mean ± std, ddof=1, NaN for a single replicate), reusing
  `replicates.annotate`. It needs **only the run's own data** — no mapping, no `phreeqc_results.csv`.
  The app renders it as a "Measured data overview — *measured data only, no model comparison*" section
  at the top of Run + Results (matplotlib, points colored by condition, time-or-condition x-axis,
  optional mean±std overlay). Covered by `tests/test_measured_overview.py`.

- **`experiments/`** (pre-data planning + QA/QC; no ML) — three independent helpers, all deriving
  their schema from `config`:
  - `plan_generator.py` expands four experiment sets (time / NaOH / CO₂ / replicate) into a run
    sheet, de-duplicating on the canonical `sample_id`
    (`CFA-NaOH{M}M-LS{ratio}-{min}min-{CO2}-R{rep}`). Plan columns match the release schema
    exactly (`fly_ash_type`), so the filled run sheet re-reads with the Phase-2 parser.
  - `validate_experimental_data.py` — `validate_experimental_df` returns a tidy issue report
    (severity `error`/`warning`/`ok`); `validate_experimental_dir` loops the measured CSVs.
  - `sustainability_score.py` — `compute_sustainability_scores` returns per-row **proxy**
    indicators (not real costs), NaN-safe on missing inputs.
  Outputs land in `outputs/tables/` (gitignored). The generated plan is added to
  `EXPERIMENTAL_NON_DATA_FILES` so the Phase-2 loader skips it.

- **`run_manager.py`** — the Experiment Run Manager (app-level "save files"). Pure file/IO,
  no chemistry. `RUN_TYPE_SPECS` maps each `run_type` (`lab_experiment`, `literature_benchmark`,
  `synthetic_demo`, `plastic_composite`) to its `data_source`, data filename, column schema, and
  warning. Typed path helpers (`lab_release_path` / `literature_path` / `demo_path`) call
  `require_run_type`, which **raises `RunTypeError`** on a mismatch — that guardrail is what keeps
  literature/synthetic data out of a lab run's `experimental_release.csv`. `create_run` writes a
  `run_config.yaml` (flat scalar mapping, serialized via a tiny built-in JSON-quoted-scalar YAML
  helper, so **no PyYAML dependency**). `export_lab_run_to_pipeline` copies a lab run's CSV into
  `data/raw/experimental_icp/experimental_release_manual_entry.csv` so the existing scripts run
  unchanged. Paths derive from `config.EXPERIMENT_RUNS_DIR`. Synthetic rows are force-tagged
  `source_type=synthetic_demo`. **Row editing:** `delete_data_rows` (by 0-based position) and
  `remove_blank_data_rows` clean a run's own CSV in place (file kept, only rows removed), for any
  run type. **Lab CSV upload:** `save_lab_dataframe(run_name, df, mode="replace"|"append")` writes
  an uploaded measured-release CSV through `lab_release_path` (so the `RunTypeError` guardrail still
  blocks literature/synthetic data); `missing_lab_required_columns` validates against
  `LAB_REQUIRED_COLUMNS` (the 10 metadata+pH columns every measured row needs). **Sample→PHREEQC
  mapping** (lab-like runs only): `add_mapping` upserts one `sample_id → phreeqc_record_key` link
  (columns `MAPPING_COLUMNS`, matching what `scripts/05` + `compare/residuals.py` expect),
  `read_mapping` / `delete_mapping_rows` / `has_mapping` manage it in
  `experiments/<run>/data/sample_phreeqc_map.csv`, `export_mapping_to_pipeline` copies it to
  `data/raw/experimental_icp/sample_phreeqc_map.csv` for step 05, and `summarize_mapping` (pure)
  reports samples / unique PHREEQC rows / samples-per-row + collisions for the mapping-quality
  warnings. **Per-run comparison artifacts + provenance** (lab-like runs only): `comparison_path` /
  `comparison_figures_dir` / `comparison_meta_path` point at `experiments/<run>/outputs/`
  (`comparison_measured_vs_phreeqc.csv`, `figures/`, `comparison_meta.json`) so one run's results can
  never display in another run's Results tab. `write_comparison_meta` stamps the run name/type, a
  timestamp, and **sha256+size fingerprints** of the three inputs (the run's data CSV, its
  `sample_phreeqc_map.csv`, and the shared `data/processed/phreeqc_results.csv`);
  `comparison_is_current(run)` re-checks those fingerprints and returns `(bool, stale_reasons)` so the
  app flags "results from older data/mappings — re-run". `scripts/05_compare_experimental.py --run
  <name>` (passed by the app's `_run_lab_workflow`) writes these per-run outputs + the stamp **in
  addition to** the global `data/processed/` + `reports/figures/` path, which still works standalone
  for the CLI-only pipeline.

- **`calculations.py`** — calculation transparency + audit (no chemistry, no ML). Pure arithmetic
  that documents and **re-derives the downstream math** the app applies on top of PHREEQC output:
  `mgl_to_mM` (uses `ATOMIC_MASSES`), `apply_dilution`, `liquid_solid_ratio`, `mass_released_mg`,
  `recovery_percent`, and `residual`. A `FORMULAS` registry of `Formula` dataclasses (equation,
  LaTeX, inputs, output, units, explanation, provenance `app-calculated` vs `parsed from PHREEQC`,
  plus a dev-mode `detail`) drives the Calculation Verification block (in the **Audit / Help** tab).
  The **audit engine**
  (`classify` / `audit_residual` / `audit_comparison`) recomputes each `measured − PHREEQC`
  residual from `comparison_measured_vs_phreeqc.csv` and labels it `pass` / `warning` / `fail` /
  `not available` against tolerances (`PASS_TOL=1e-6`, `WARN_TOL=1e-4`). It **explains** PHREEQC's
  saturation index and pH but never recomputes them — PHREEQC stays authoritative. Covered by
  `tests/test_calculations.py`.

- **`scenarios.py`** — PHREEQC scenario manifest + rule-based mapping assistant (no chemistry, no
  ML). `build_scenario_manifest` / `write_scenario_manifest` turn `phreeqc_results.csv` into
  `data/processed/phreeqc_scenario_manifest.csv` (`MANIFEST_COLUMNS`): molality→mM via
  `config.PHREEQC_MOLALITY_TO_MM` (missing `mol_Fe` → NaN, not zero), a readable `scenario_label`,
  and metadata `infer`-red from the source filename only where safe (`L-S_5`→L/S 5; `atmCO2`→
  `atm_CO2`; `lowCO2`→`low_CO2`; `noCO2`→`sealed`; else `unknown`, with `metadata_quality`
  good/partial/unknown). `score_scenario` applies **hand-written** rules (+3 batch / −4 initial /
  +3 L/S match / +2 compatible CO₂ / +1 temp match-or-unknown / −2 major conflict); `confidence_for`
  bands the raw score high/medium/low (`HIGH_SCORE=7`, `MEDIUM_SCORE=4`), but `_metadata_alignment`
  then **caps** confidence to *medium* whenever the experiment specifies metadata the PHREEQC
  manifest lacks — `time_min`, an OA/PF/GS `condition_code` (`sample_condition_code` reads
  `extra__condition_code`/`sample_id`/`notes`), or `NaOH_M` — so **high requires both sides to
  align**, not just L/S + CO₂ + batch. The cap returns `base_confidence`, `phreeqc_missing`, and
  human `metadata_notes` (e.g. "Experimental time is known, but the selected PHREEQC scenario does
  not specify time.") that the Match PHREEQC tab shows per suggestion. `score_scenario` returns a
  machine-readable **decision `trace`** — one entry per rule that fired
  (`{field, sample_value, scenario_value, outcome: matched|missing|conflict|normalized, points, note}`,
  incl. fuzzy normalizations like HCl→acid and CO₂ family grouping, and 0-point metadata-quality cap
  entries). The flat `reason`, the `matched_fields`/`mismatched_fields`/`missing_metadata` lists, the
  `score_breakdown` (`{rule, delta}`) and the `confidence_explanation` ("score 9 of max 9 → high;
  capped to medium because …") are all **derived from the trace** (one code path; `score` always equals
  the sum of trace `points`) — so the UI explanation is generated, not re-derived. **Scoring weights
  unchanged** — this was pure restructuring. Methodology write-up for scientists in
  `docs/mapping_rules.md`. `suggest_mappings` returns the
  top-N scenarios, `samples_needing_simulation` flags unmapped/low-confidence/colliding samples, and
  `describe_solutions` / `load_solution_descriptions` summarise each `.pqi` `SOLUTION n` label so the
  app can explain that **sol1/sol2/sol3 are PHREEQC solution numbers, not time points or replicates**
  unless the input defines them so. Covered by `tests/test_scenarios.py`.

- **`import_mapping.py`** — flexible experimental-file import (ingest helper, no chemistry, no ML).
  Pure functions the Data-tab lab uploader wires to widgets: `file_kind` / `list_excel_sheets` /
  `read_tabular` read `.csv`/`.xlsx`/`.xls` (one Excel sheet at a time); `suggest_column_mapping`
  maps uploaded headers onto the release schema (`MAPPING_TARGETS` = `EXPERIMENTAL_RELEASE_COLUMNS`
  + optional `leachant`/`acid_M`) via **hand-written** `COLUMN_SYNONYMS` (two passes — exact name
  wins over alias, one source column used once); `convert_concentration` / `convert_series_to_mM`
  convert mg/L·ppm·ppb → mM reusing `calculations.ATOMIC_MASSES` (`mM = mg/L / atomic_mass`,
  ppb = µg/L; Sc/REE stay ppb). `build_schema_frame` produces a schema-aligned frame: chemistry
  unit-converted, `leachant`/`acid_M` filled (acid rows — `is_acid_leachant` — get `NaOH_M` blanked
  + an `ACID_IMPORT_NOTE`, never forced into `NaOH_M`), provenance (`PROVENANCE_COLUMNS`:
  file/sheet/row/timestamp/warning/units) and unknown columns (`extra__` prefix) appended so
  nothing is dropped silently. `summarize_import` is the pre-save report (missing required, pH
  outside 0–14, blank/duplicate sample_ids, no-measured-value rows, converted columns, row
  classification pH-only/chemistry-present/incomplete). The extra columns ride through
  `run_manager.save_lab_dataframe` (which keeps non-canonical columns after the schema), so the
  existing pipeline is unaffected. Covered by `tests/test_import_mapping.py`.

- **`dissolution_workbook.py`** — special-case parser for the Class C fly ash **dissolution
  workbook** (ingest helper, no chemistry, no ML), an *additional* Data-tab import mode beside the
  generic `import_mapping` one (never a replacement). The workbook is non-rectangular. The **ICP OES**
  sheet lays unit groups out **horizontally**: a single top row holds `mg/L` and `mmol/l`, each
  anchoring a group of condition columns (`NaOH-OA/PF/GS`); each element block (Calcium/Silicon/
  Aluminum) then has one shared header row spanning *both* unit groups, with reaction-time rows. The
  **pH** sheet has a `Sample | Time (min) | pH` label list (incl. HCl rows) plus a NaOH pH matrix.
  Parsing is **marker-based**, not fixed cell coordinates (`ELEMENT_TO_COLUMN`, `_match_unit`,
  `CONDITION_RE`, `LABEL_RE`): `_unit_column_map` assigns each column to its unit group from the
  global unit row; `parse_icp_sheet` reads each block long, picks each cell's unit by column, and
  **prefers `mmol/l`** (converting `mg/L`→mM via `import_mapping.convert_concentration` only as a
  fallback); `parse_ph_sheet` reads pH from the **pH header column** (not the Time column) for
  explicit labels, plus a NaOH pH-matrix pass for matrix-only times (e.g. 20 min). `"-"`/blank cells
  are treated as **missing, never 0**. `normalize_dissolution_workbook` joins chemistry onto NaOH pH
  rows by `(condition_code, time_min)`, keeps **HCl rows pH-only + acid-tagged** (`NaOH_M` blank,
  `acid_M` set, `ACID_IMPORT_NOTE`), fills operator-supplied metadata from a `defaults` dict
  (`DEFAULT_FILL_FIELDS`; `fly_ash_type` defaults `Class C fly ash`), and honours an `include_hcl`
  scope. It returns `(schema_df, report)` where `report` has parse counts
  (NaOH/HCl/with-pH/with-chem/missing-metadata), warnings (Fe/Na/K/Sc/REE absent; OA/PF/GS are
  cup-cover/CO₂ conditions — OA open air, PF plastic flap, GS glass cover, PF/GS not sealed unless
  confirmed; HCl ≠ NaOH PHREEQC), and `icp_debug` (per-element time×condition pivots for the app's
  debug view, via `icp_debug_pivots`). OA/PF/GS are preserved in `sample_id`, `notes`, an
  `extra__condition_code` column, and optional derived `extra__cover_condition` /
  `extra__CO2_exposure_level` columns (from `scenarios.cover_condition` / `scenarios.co2_exposure_level`). Reuses `import_mapping`'s leachant/provenance columns so saved rows
  match a generic import. Validated against the real workbook layout and a synthetic fixture in
  `tests/test_dissolution_workbook.py`; the marker constants (sheet/element/unit/condition labels)
  are the tuning points if another workbook differs.

- **`replicates.py`** — replicate-aware mapping layer (no chemistry, no ML). In this project PHREEQC
  `sol1/sol2/sol3` are **replicate batches of one experimental condition**, not time points, so a
  measured row is *(condition, replicate batch)* rather than a sample mapped straight to a solution
  number. `condition_key` collapses leachant + molarity (`acid_M` for acids) + OA/PF/GS code + time +
  L/S + CO2 + temp into one stable grouping key (e.g. `NaOH0.5M_OA_10min_LS5_open`), optionally
  appending `_batch<id>` when the profile sets `group_by_batch` (see Prompt-21 bullet); `replicate_id` /
  `parse_replicate_id` read `R1/rep2/batch3` from the sample_id (`infer_replicate_ids` fills blanks by
  order **with a warning**); `replicate_summary` reports count + **mean ± std ± SEM** (ddof=1; SEM=std/√n;
  NaN for a single replicate) of pH/Ca/Si/Al per condition. `expand_condition_mapping` turns one `condition_key →
  record_key` link into the per-sample map the pipeline reads (all replicates inherit it);
  `replicate_record_key` / `expand_replicate_solution_mapping` are the optional advanced path where
  each replicate points at its own `solN`. `condition_mean_comparison` (mean ± std vs PHREEQC,
  `residual = mean − PHREEQC`, n<2 flag) and `individual_replicate_comparison` are the two results
  modes. `collision_report` is replicate-aware: same-condition replicates sharing a PHREEQC scenario
  is **expected** (not flagged); it warns only on **different** condition_keys sharing a scenario,
  acid→NaOH mappings, and (via `scenarios._metadata_alignment`) time/condition metadata PHREEQC
  can't confirm. Storage lives in `run_manager` (`condition_phreeqc_map.csv`,
  `replicate_solution_map.csv`; `add_condition_mapping(..., notes="", override=False)` upserts
  optional free-text `notes` + a boolean `override` column (`override=true` marks a deliberately-saved
  unsafe mapping from the manual-override path) — both back-compat on read, both stay in the condition
  map and never reach the 2-column per-sample map; `apply_condition_mapping` expands to the
  run's `sample_phreeqc_map.csv`). Covered by `tests/test_replicates.py`. Surfaced in the Match
  PHREEQC tab (replicate summary + condition-level mapping + advanced replicate→solution expander +
  replicate-aware collision warnings) and Run + Results (comparison-mode radio + condition mean±std
  error-bar plot + individual-replicate scatter). For **presentation honesty**, `mapping_status`
  classifies a sample→scenario link as `exact` / `scenario-level only` / `unsafe` / `needs new
  simulation` (`MAPPING_STATUS_DEFINITIONS`, worded generically as measured-data↔model-prediction),
  `overall_mapping_status` aggregates it (with
  `all_exact`), and `conditions_needing_simulation` is the presentation table
  (`CONDITIONS_NEEDED_COLUMNS`). The Start tab's **Presentation summary** (`_render_presentation_summary`)
  surfaces dataset/validation/mapping counts, overall mapping + comparison status, a recommended next
  *scientific* step, and the standing caveat that the comparison is **preliminary / a workflow check
  unless mappings are exact**; the same valid-now / not-yet wording and status definitions appear in
  Audit / Help, and Run + Results shows the "residual plots are a workflow check, not final
  validation" warning whenever any mapping is not exact.

- **`mapping_table.py`** — consolidated suggestion table (no chemistry, no ML; bridges
  `replicates` + `scenarios`, so it lives in its own module to avoid the `scenarios`↔`replicates`
  import cycle). `build_suggestion_table(data, manifest, existing_mapping)` groups measured rows by
  `replicates.condition_key` (mapping stays condition-level with replicate inheritance), scores each
  condition's representative row via `scenarios.suggest_mappings`, classifies the best candidate with
  `replicates.mapping_status`, and returns one row per condition (`SUGGESTION_TABLE_COLUMNS`:
  condition_key, n_replicates, scenario_label, phreeqc_record_key, mapping_status, score, confidence,
  reason, already_mapped). `exact_suggestions` (bulk-accept filter — `BULK_ACCEPT_STATUS` = exact),
  `SELECTABLE_STATUSES` (exact + scenario-level; **unsafe excluded**), `needs_new_simulation` (drives
  the conditions-needing-simulation section from the same table), and `condition_candidates` (the
  representative sample + top-N scored candidates, each with `score_breakdown`, for the row-detail
  view). Pure; does no scoring of its own. Covered by `tests/test_suggestion_table.py`.

- **`flyash_phreeqc_ml/agent/`** — the **AI agent orchestration layer** behind the Assistant tab (off the
  scientific result path). Wraps an LLM around the deterministic Simulate backend: per turn `nlu_extractor`
  **understands** the (possibly messy) message and the model **proposes one structured action**, `agent_policy`
  **gates** it (execution/save require explicit confirmation; PHREEQC blocked for non-leaching /
  missing-composition), and `tool_registry` runs the **existing** deterministic functions. **`nlu_extractor.py`**
  (new — see the *Robust NLU* completed-phase bullet) is the AI-first understanding layer (it mirrors
  `ai/scenario_parser`): one grounded call returns a structured `understanding` block **and** the action, then
  deterministic code **validates/normalizes** it (typo+unit normalization, canonicalization, impossible-value
  rejection, assumption flagging, change/conflict detection) — falling back to a robust rule-based parse with no
  key. **`agent_council.py`** (new — see the *Agent Council v1* bullet) is the advisory review layer: after the
  orchestrator chooses an action, `run_council` produces five role assessments + one synthesis (deterministic
  baseline + an optional AI enrichment merged so the canonical engine-status / scientific-warnings / safe-action
  stay code-generated) — it **runs nothing, decides nothing** (imports no executor and no tool registry;
  `safe_next_action` only mirrors the orchestrator). Pure modules (`agent_state` / `agent_actions` /
  `agent_prompts` / `agent_policy` / `domains`) import no AI and no executor; the **three AI-touching modules are
  `agent_orchestrator` + `nlu_extractor` + `agent_council`** (all three import no executor); only `tool_registry`
  touches the executor; **no scientific/result-path module imports the agent**. `agent_state.apply_delta` is the
  pure correction-aware merge; `agent_state.last_council` is a duck-typed `object` (so `agent_state` stays
  AI-free); `domains.py` owns the planning-support metadata for non-executable domains. With no key a
  deterministic parser + planner + council drives the same flow. See the **agent / Materials-Research-Assistant /
  Robust NLU / Agent Council** completed-phase bullets above; docs `docs/assistant_agent.md`; boundaries pinned by
  `tests/test_ai_boundary.py`.

- **`app.py`** (repo root) is a **thin entry point** (~210 code lines): the `sys.path` bootstrap, the
  run-management **sidebar** (`_render_run_sidebar` — now the **only** top-level render function) + the
  **four-section nav** (`st.sidebar.radio(key="nav_section")`), page config + hero, and inline **dispatch** to
  `ui.<module>.render(...)`. **All workflow rendering lives in the `ui/` package** (see the *UI modularization*
  + *Assistant-first redesign* + *Dark research-cockpit makeover* completed-phase bullets +
  `docs/refactor_plan.md`): one module per workflow/section (`ui/<name>_tab.py` + `ui/results.py` /
  `ui/engine_library.py` / `ui/settings.py`, each exposing `render`) plus shared `ui/state.py` /
  `ui/common.py` / `ui/formatters.py`, with the **dark** design system in `app_ui.py` (+ base palette in
  `.streamlit/config.toml`). The app is presented as a **Materials Research Assistant** with a **dark
  seven-section left-rail navigation** — **Assistant · Workspace · Results · Data & Validation · Projects ·
  Engine Library · Settings** — and the **Assistant** (`ui/assistant_tab.py`, a two-column chat workspace) is
  the main product / front door. The other sections hold every advanced workflow (none removed): **Workspace**
  = the full manual Simulate builder (`simulate_tab`: NL → scenario → plan → material/release/database → input
  preview → **gated PHREEQC run + small sweep + plots → ranking / target matching → saved provenance** — plan
  generation runs nothing, execution is a separate confirmed step, every output is a *prediction, not
  validated*); **Results** (`results`) is a clean read-out of the latest prediction (not validation);
  **Data & Validation** holds Import/Validate/Match/Compare sub-tabs (the measured-vs-model mapping +
  comparison is the **Validation module**, the current strongest workflow); **Projects** holds `export_tab`
  (saved runs / report / audit / guide); **Engine Library** (`engine_library`) is the modular engine registry;
  **Settings** (`settings`) holds the AI provider/model panel + PHREEQC status + dev-mode + the future
  architecture. Every section carries a one-line header
  + a **➡️ Next step** hint and a specific empty state. **The numbered list below predates the Simulate
  execution arc, the identity renovation, the Prompt-20 reorg, AND the UI modularization — it documents the
  underlying render functions (now living in `ui/<tab>_tab.py`, not `app.py`), not the current tab names/order;
  see the completed-phase bullets above for the current layout:**
  1. **Start** (`_render_overview`) — project status cards + selected-run summary (run type, data
     rows, mapped samples, unique PHREEQC rows used), a one-line **data-quality status**, what's
     missing, a **recommended next action** (no-data / no-mapping / coarse-mapping / workflow-not-run
     / ICP-missing / mock-data cases), and a **workflow checklist** (Data uploaded → Data checked →
     Mapping complete → Workflow run → Results available).
  2. **Data** (`_render_data_entry_tab`) — run-type specific: lab measured-release form **plus a
     two-mode "Upload experimental data file"** importer (`_lab_data_import` mode radio):
     **Generic table** (`_generic_table_import`, `.csv`/`.xlsx`/`.xls` via `import_mapping`: raw
     preview → Excel sheet pick → column mapping → unit conversion → leachant/provenance → pre-save
     validation → confirm-gated replace/append) **or Class C fly ash dissolution workbook**
     (`_dissolution_import` via `dissolution_workbook`: shared metadata defaults → NaOH-only/NaOH+HCl
     scope → marker-based parse → normalised preview with parse counts + warnings → confirm-gated
     save) — literature CSV upload + manual rows, or synthetic/demo form — plus this run's table,
     row deletion, CSV/pipeline export, a **basic validation summary** (error/warning counts via the
     07 validator, lab runs), and the **legacy global manual-entry form** under a "not recommended"
     expander.
  3. **Match PHREEQC** (`_render_mapping_tab`, lab-like runs only) — a **PHREEQC Scenario Explorer**
     (filterable manifest table), a **Mapping Assistant** (pick a sample → top-3 rule-scored
     suggestions with confidence + "Use this mapping" buttons, a no-good-match warning), a
     mapping-quality summary with collision/coarse warnings, a "samples needing new PHREEQC
     simulations" table, existing-mapping upsert/preview/delete/export, and the original dropdown
     kept under an **"Advanced manual mapping"** expander.
  4. **Run + Results** (`_render_run_and_results_tab`) — combines workflow execution and results: one
     primary button that, for a lab run, exports the run CSV + mapping then runs Phase 1 → 07 → 05 →
     08, stopping at the first failure, warning if no mapping (plus an "Advanced individual script
     controls" expander); then the run-type-aware results — lab shows the measured-vs-PHREEQC
     summary, comparison/residual figures, an interpretation note on coarse mapping, pH residual
     cards, validation + sustainability tables; literature shows its own benchmark summary; synthetic
     shows a testing-only warning.
  5. **Audit / Help** (`_render_audit_help_tab`) — the Calculation Verification block (formula
     registry, per-row residual audit, mg/L→mM and L/S calculators, developer-mode explanations),
     the **PHREEQC raw outputs** (processed-CSV previewer + PHREEQC-**only** model-output figure
     viewer) under expanders, and the **Help / Safety** reference (workflow, run types, mapping,
     residuals, limitations).

  It reuses package functions and adds no chemistry/ML logic. Tables are height-limited so they don't
  stretch the page; advanced content (raw PHREEQC tables, individual scripts, formula-audit details,
  legacy/global data entry, advanced manual mapping) lives in expanders. The legacy form appends to
  `data/raw/experimental_icp/experimental_release_manual_entry.csv` (gitignored); the run workspace
  writes into the selected run's own `experiments/<name>/data/`. **Experiment-plan generation (06)
  is not surfaced in the UI** — the app no longer creates plans.

### Key conventions

- A PHREEQC solution state is identified by `record_key` = `"<file>|sim<N>|<state>|sol<N>"`; this
  is the join key between PHREEQC results and measured samples.
- Comparisons default to PHREEQC `state == "batch"` (the post-equilibration result that an
  experiment measures).
- Phase 2 is built to be a no-op until data lands: `run_phase1.py` is untouched by Phase 2, and
  step 05 detects a blank template and exits cleanly. Keep this separation when extending.
- **CO₂ condition = cup-cover vocabulary.** `config.CO2_CONDITION_ALLOWED` =
  `["OA","PF","GS","atm_CO2","low_CO2","no_CO2","unknown"]`. CO₂ exposure is controlled by the cup
  cover — **OA** = open air (atmospheric CO₂), **PF** = plastic flap cover, **GS** = glass cover;
  PF/GS likely reduce CO₂ exchange but are **not confirmed airtight — never called "sealed"** in
  code/UI/plots/docs. `atm_CO2`/`low_CO2`/`no_CO2` are the *model-side* (PHREEQC scenario) labels.
  The validator errors on anything else; the plan generator, the Streamlit dropdown (derived from
  this list) and sample entry use these exact labels. `config.CONDITION_CODE_DESCRIPTIONS` is the
  single source of truth for the human descriptions + the not-confirmed-sealed caution (the UI reads
  it). The legacy `open`/`sealed` labels were removed: importers map legacy `open`→`OA` (with a note)
  and **flag** legacy `sealed` for the user to resolve (PF vs GS is not knowable — never auto-mapped).
  `co2_family` classifies two families — **atmospheric** (OA, atm_CO2) and **reduced** (PF, GS,
  low_CO2, no_CO2). For matching, **OA can reach `exact`** against an atmospheric model scenario, but
  **PF/GS cap at `scenario-level only`** (reduced but unconfirmed) until a model scenario explicitly
  carries that cover code; cross-family (OA↔reduced, PF/GS↔atmospheric) is `unsafe`.
- **Fe is often unpredicted.** The CEMDATA18 runs may omit `mol_Fe`, so `phreeqc_Fe_mM` and
  `residual_Fe` can be entirely NaN. Step 05 prints an explicit WARNING when Fe is *measured* but
  PHREEQC has no Fe prediction — this is "unavailable", not "PHREEQC predicts zero Fe". The scenario
  manifest follows the same rule: a missing molality column → NaN prediction, never zero.
- **The app does not generate experiment *run sheets* (but it does plan + run simulations).**
  Experiment-**plan**-sheet generation (`scripts/06_generate_experiment_plan.py` — the bench run sheet) was
  removed from the UI; the script still exists CLI-only, no button surfaces it. The app's data-validation
  purpose remains ingest → verify → map → run → interpret. Separately, the **Simulate** tab *does* plan **and**
  (on a gated, confirmed click) run a **simulation** — these are different things: a *bench experiment plan*
  (06, CLI-only) vs. a *PHREEQC simulation* (Simulate, in-app). The Simulate forward arc is the current
  development focus.
- **Scenario metadata is inferred conservatively.** `scenarios.infer_metadata_from_filename` only
  reads tokens it is sure of (`L-S_<n>`, `atmCO2`/`lowCO2`/`noCO2`); everything else (notably
  `NaOH_M`, never in these filenames) stays `unknown` rather than being guessed. The mapping
  assistant's scores are **hand-written rules, not learned weights** — keep them transparent.
- The `sample_id` format `CFA-NaOH{M}M-LS{ratio}-{min}min-{CO2}-R{rep}` (built by
  `plan_generator.make_sample_id`) is the human-facing link from run sheet → filled release CSV →
  `sample_phreeqc_map.csv` → comparison. It's the dedup key in the plan (replicates kept distinct),
  so keep it stable.
