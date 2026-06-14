# app.py refactor plan (preparation only — not yet executed)

`app.py` is ~4,370 lines: a six-tab Streamlit workflow (Start · Import · Validate ·
Match · Compare · Export) plus ~120 helper/render functions. Before the
simulation / mass-balance / AI expansion, the tab bodies should move into a `ui/`
package so app.py becomes a thin wiring layer. **This file is the plan; only the
trivially-safe parts (a `ui/` package + a few pure helpers) have been executed so far
— every `_render_*` tab function stays in app.py for now.**

## Target module map

| Future module | Current tab / responsibility | Main `_render_*` functions to move (later) |
| --- | --- | --- |
| `ui/start_tab.py` | Start (overview + checklist + next-step) | `_render_start_tab`, `_render_overview`, `_render_presentation_summary` |
| `ui/data_tab.py` | Import (data entry, file import, unit review) | `_render_import_tab`, `_lab_data_import`, `_generic_table_import`, `_dissolution_import`, `_render_model_predictions_import`, `_render_run_data_and_edit`, `_lab_entry_form`, `_literature_entry`, `_demo_entry`, `_render_legacy_global_form`, `_render_ai_import_assist` |
| `ui/simulate_tab.py` | Simulate (PHREEQC run + surrogate; **grows with the new sim/mass-balance work**) | `_render_run_workflow_tab`, `_render_generate_simulation`, `_render_surrogate_expander` |
| `ui/mapping_tab.py` | Match (measured ↔ model mapping) | `_render_match_tab` + its mapping/suggestion helpers |
| `ui/results_tab.py` | Validate + Compare (overview, comparison, residuals, bias, correction) | `_render_validate_tab`, `_render_measured_overview`, `_render_overview_plot`, `_render_basic_validation_summary`, `_render_compare_tab`, `_render_results_tab`, `_render_condition_results`, `_render_condition_errorbar`, `_render_comparison_inclusion`, `_render_systematic_bias`, `_render_residual_correction`, `_render_comparison_figures` |
| `ui/audit_tab.py` | Validate (calc verification) + Export (report, audit trail, help, assistant) | `_render_calc_verification_tab`, `_render_unit_registry`, `_render_conversion_verification`, `_render_unit_calculator`, `_render_processed_viewer`, `_render_phreeqc_only_figures`, `_render_export_tab`, `_render_export_report`, `_render_audit_trail`, `_render_user_guide`, `_render_help_tab`, `_render_assistant` |

`app.py` would keep: page config, the run-management sidebar, `DEV_MODE`, the
`st.tabs([...])` wiring, and shared cross-tab constants (`_PROJECT_ROOT`, `MODEL_NAME`,
the cached `_read_csv` / `_scenario_manifest`, `_manifest_if_available`).

## Why this is deferred (not done now)

Moving a `_render_*` function is **not** trivially safe today because most of them
reference app-level shared state that would have to move or be injected first:

- **Streamlit + `st.session_state`** keys (widget keys, the `_audit_seen` set, report
  zip blobs) are scattered through the render functions.
- **Module-level caches** `@st.cache_data`-decorated `_read_csv`, `_scenario_manifest`
  and helpers like `_manifest_if_available`, `_next_step_hint`, `_run_type_warning`.
- **Shared constants** `_PROJECT_ROOT`, `MODEL_NAME`, `_ICP_MEASURED_COLS`,
  `WORKFLOW_STEPS`, `MANUAL_ENTRY_PATH`.

The safe sequence for the real refactor (a later, dedicated PR):
1. Extract shared constants + cached readers into `ui/state.py` (no Streamlit widget
   calls), import them back into app.py.
2. Move one tab at a time (start → data → … ), running `compileall` + `pytest` +
   the AppTest smoke after each move.
3. Keep the `st.tabs([...])` wiring in app.py calling `ui.<tab>.render(selected_run)`.

## Executed now (trivially safe only)

- Created the `ui/` package (`ui/__init__.py`, `ui/formatters.py`).
- Moved **only pure, self-contained formatters / data-prep** (no Streamlit, no
  `session_state`, no app-level globals) into `ui/formatters.py`, re-imported into
  app.py so all call sites are unchanged:
  - `is_present(value)` — non-blank cell check.
  - `has_numeric(df, col)` — column has ≥1 numeric value.
  - `nearest_manifest_row(manifest, naoh, ls)` — closest batch scenario (display only).

## Deferred move candidates (pure-ish but NOT moved — need a shared seam first)

These read app-level globals or cached readers, so moving them now would create churn
or coupling; they move with their tab in the real refactor:

- `_rel(path)` — needs `_PROJECT_ROOT`.
- `_next_step_hint`, `_has_numeric`-using helpers, `_inclusion_variables`,
  `_residual_elements_with_data`, `_residual_col`, `_variable_element` — read
  `_read_csv` / `config` / `compare_inclusion` module references.
- All `_render_*` — Streamlit + session-state bound (move per the sequence above).
