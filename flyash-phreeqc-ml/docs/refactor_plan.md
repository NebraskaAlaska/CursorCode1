# UI architecture — app.py + the `ui/` package (refactor executed)

`app.py` was a ~6,650-line Streamlit script holding the whole seven-tab workflow plus ~165
helper/render functions. It has been split into a thin entry point (`app.py`) plus a `ui/`
package of tab modules. **Behavior is unchanged** — the split was a verbatim move (every
function body and constant value is byte-identical to the pre-refactor `app.py`, every
session-state key and widget label is preserved), verified by `tests/test_ui_modularization.py`,
`tests/test_app_tabs_smoke.py`, and the full suite.

## Where things live now

| File | Responsibility |
| --- | --- |
| **`app.py`** (~210 code lines) | Thin entry point: page config + hero, the run-management **sidebar** (`_render_run_sidebar`), the **AI settings** panel (`_render_ai_settings_panel`), `DEV_MODE`, and the `st.tabs([...])` **dispatch** to `ui.<tab>.render(...)`. |
| **`ui/state.py`** | Shared cross-tab **state** — constants (`MODEL_NAME`, `_PROJECT_ROOT`, `_ICP_MEASURED_COLS`, the validity-wording constants, the figure-name sets, …), paths, and cached data readers (`_read_csv`, `_scenario_manifest`, `_manifest_if_available`, `_rel`, `_next_step_hint`, …). No tab-specific rendering. |
| **`ui/common.py`** | Shared **render helpers** used by more than one tab (`_render_next_step`, `_render_valid_now_section`, `_render_mapping_status_definitions`, `_png_provenance_caption`, `_audit_once`). |
| **`ui/formatters.py`** | Pure formatters / data-prep (no Streamlit). |
| **`ui/start_tab.py`** | Start tab — overview, product modes, workflow checklist, next-step. |
| **`ui/simulate_tab.py`** | Simulate tab — describe → parse → plan → material/release/database → input preview → gated run + plots → ranking/refinement → target matching. (Largest module.) |
| **`ui/import_tab.py`** | Import Data tab — measured-data entry + flexible/dissolution file import. |
| **`ui/validate_tab.py`** | Validate tab — measured overview, basic validation, calc verification, model-output viewer. |
| **`ui/match_tab.py`** | Match tab — measured ↔ model-prediction mapping (suggestions + manual). |
| **`ui/compare_tab.py`** | Compare Results tab — run workflow + comparison/residuals/bias/correction + assistant + surrogate. |
| **`ui/export_tab.py`** | Export tab — validation report, audit trail, previous simulation runs, user guide. |

Each `ui/<tab>_tab.py` keeps its original `_render_<tab>_tab` function (unchanged name + body)
and exposes it as **`render`** (an alias), which `app.py` calls: `ui.start_tab.render(run)`.

## Where the *scientific* logic lives (unchanged)

All chemistry / statistics / ML / parsing lives in the **`flyash_phreeqc_ml/`** package
(parsers, `compare/`, `scenarios`, `replicates`, `mapping_table`, `mass_balance`, `attribution`,
`simulation/`, `ml/`, `ai/`, …). The UI **calls** these modules to render; the science never
calls the UI. This direction is enforced by
`tests/test_ui_modularization.py::test_scientific_package_does_not_import_ui_or_app`.

## The import graph (an acyclic DAG)

```
app.py ──▶ ui/<tab>_tab.py ──▶ ui/common.py ──▶ ui/state.py ──▶ ui/formatters.py
                          └────────────────────▶ ui/state.py
all ui modules ──▶ flyash_phreeqc_ml/**   (science; never imports ui)
```

- A tab module imports only `ui.state`, `ui.common`, `ui.formatters`, and scientific modules.
- `ui.state` / `ui.common` never import a tab module; no tab imports another tab.
- The partition was computed by **reachability** from the seven tab entry points: a helper
  reached by exactly one tab lives in that tab; a helper reached by ≥ 2 tabs (or the sidebar)
  is shared (`ui.state` / `ui.common`). This guarantees the DAG above with no cycles.

## Beyond the UI: simulation logic, the result path, AI, and outputs

The `flyash_phreeqc_ml/` package is the science, organized so the UI only *calls* it:

- **`flyash_phreeqc_ml/simulation/`** holds the **scientific simulation logic** — scenario schema,
  rule parser, plan matrix, **source terms** (release model), **database compatibility** + **phase
  templates**, the deterministic **input builder**, the gated **executor** + **batch executor**,
  **strategy** (ranking) + **target matching**, and the **run registry**. PHREEQC input generation
  and execution live here, not in the UI.
- **The validation / result-path modules stay separate** — `compare/` (residuals + inclusion /
  validity), `scenarios`, `replicates`, `mapping_table`, `mass_balance`, `attribution`, `viz/`.
  These compute mapping, residuals, and the validity status. They import **no UI and no AI**.
- **AI modules (`flyash_phreeqc_ml/ai/`) are suggestion-only and off the result path.** They
  propose interpretations (import mapping, scenario extraction, literature retrieval, Q&A) but
  never compute mapping/residuals/validity and **never write PHREEQC input**. This boundary is
  pinned by `tests/test_ai_boundary.py` (an AST import scan: the result-path + simulation modules
  import no AI/executor; the AI is never on the scientific path).
- **Generated outputs stay under `outputs/`** (`outputs/simulations/` for the execution workspace,
  `outputs/simulation_runs/` for saved provenance bundles, `outputs/tables/`, `outputs/figures/`),
  plus `data/processed/` and `experiments/<run>/` — all gitignored and re-creatable. No module
  writes generated artifacts into `data/raw/` or the source tree; the executor and run registry
  enforce a safe-workspace check.

So the full dependency arrow is one-way: **`app.py` → `ui/` → `flyash_phreeqc_ml/`**, with AI a
suggestion-only side input and generated artifacts confined to `outputs/`.

## How to add a new UI section safely

1. **Put rendering in the right tab module.** Add helper + render functions to the relevant
   `ui/<tab>_tab.py`. Keep them Streamlit-only — call into `flyash_phreeqc_ml/` for any
   calculation; never compute results in the UI.
2. **Share via state/common, not across tabs.** If two tabs need the same helper, put it in
   `ui/common.py` (render) or `ui/state.py` (constants / cached readers / pure helpers) and
   import it. **Never import one tab module from another** (the DAG test will fail).
3. **Never import a UI module from `flyash_phreeqc_ml/`.** The science stays UI-free.
4. **Keep `app.py` thin.** Only page config, the sidebar, and the tab dispatch belong there.
   New tabs are wired as `with tab_x: x_tab.render(...)`.
5. **Run the guards:** `pytest tests/test_ui_modularization.py tests/test_app_tabs_smoke.py`
   (plus the full suite) — they check the import graph, that every module imports cleanly and
   exposes `render`, that `app.py` stays thin, and that all seven tabs render end-to-end.

## Why `app.py` must stay thin

`app.py` is a Streamlit *script* (it runs `st.set_page_config` at import, so it cannot be
imported as a module — only executed). Keeping logic out of it means: render functions are
importable + unit-testable (the `ui/` modules import cleanly without a Streamlit runtime), the
seven tabs are independently navigable in the code, and a change to one tab touches one file.
The entry point's only job is wiring: sidebar → run selection → dispatch.
