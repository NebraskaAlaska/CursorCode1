"""Import Data tab — measured-data entry + flexible/dissolution file import.

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

import io
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import app_ui  # noqa: E402  (presentation-only UI helper layer)
from flyash_phreeqc_ml import audit  # noqa: E402  (append-only audit log)
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import dissolution_workbook  # noqa: E402
from flyash_phreeqc_ml import import_mapping  # noqa: E402
from flyash_phreeqc_ml import profiles  # noqa: E402
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml import scenarios  # noqa: E402
from flyash_phreeqc_ml import units  # noqa: E402  (single conversion authority)
from flyash_phreeqc_ml.ai import import_assist  # noqa: E402  (optional AI helpers)

from ui.common import _render_next_step
from ui.state import MANUAL_ENTRY_FILENAME, _read_csv, _rel, _scenario_manifest

MANUAL_ENTRY_PATH = config.EXPERIMENTAL_ICP_DIR / MANUAL_ENTRY_FILENAME

# Free-text columns get plain text inputs; a couple get friendly dropdowns.
# CO2 labels come straight from config so the form, validator, and plan agree.
_CO2_OPTIONS = [""] + list(config.CO2_CONDITION_ALLOWED)

_YESNO_OPTIONS = ["", "yes", "no"]

# --------------------------------------------------------------------------- #
# Experiment-run sidebar + workspace (the "save files" layer)
# --------------------------------------------------------------------------- #
def _run_type_warning(run_type: str) -> None:
    """Render the run-type warning with severity matching its meaning."""
    msg = run_manager.warning_for(run_type)
    if run_type == "lab_experiment":
        st.info(f"🧪 {msg}")
    elif run_type == "literature_benchmark":
        st.warning(f"📚 {msg}")
    elif run_type == "synthetic_demo":
        st.error(f"🧩 {msg}")
    else:  # plastic_composite
        st.warning(f"♻️ {msg}")

def _import_raw_frame(run_name: str, up) -> tuple[pd.DataFrame | None, str, str]:
    """Read an uploaded CSV/Excel into a raw frame (handles sheet selection).

    Returns ``(raw_df_or_None, kind, sheet_name)``. Renders the file-type / sheet
    widgets and any read error inline. ``raw_df`` is None when the file can't be
    read yet (bad type, unreadable, or empty).
    """
    try:
        kind = import_mapping.file_kind(up.name)
    except import_mapping.ImportMappingError as exc:
        st.error(str(exc))
        return None, "", ""

    data = up.getvalue()
    sheet_name = ""
    if kind == "excel":
        try:
            sheets = import_mapping.list_excel_sheets(io.BytesIO(data))
        except import_mapping.ImportMappingError as exc:
            st.error(str(exc))
            return None, kind, ""
        sheet_name = st.selectbox(
            "Select sheet", sheets, key=f"lab_import_sheet_{run_name}",
            help="Excel workbooks can hold several sheets — pick the one to import.",
        )

    try:
        raw = import_mapping.read_tabular(io.BytesIO(data), kind=kind, sheet=sheet_name or None)
    except import_mapping.ImportMappingError as exc:
        st.error(str(exc))
        return None, kind, sheet_name
    except Exception as exc:  # pragma: no cover - UI guard
        st.error(f"Could not read file: {exc}")
        return None, kind, sheet_name

    if raw.empty:
        st.warning("The selected file / sheet has no rows.")
        return None, kind, sheet_name
    return raw, kind, sheet_name

def _import_render_report(report: dict) -> None:
    """Render the pre-save validation summary (Feature 7) inline."""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows to save", report["n_rows"])
    c2.metric("Rows missing required", report["rows_missing_required"])
    c3.metric("Blank sample IDs", report["blank_sample_ids"])
    c4.metric("Rows with no values", report["rows_no_measured_values"])

    if report["missing_required_columns"]:
        st.error("Missing required column(s): "
                 + ", ".join(f"`{c}`" for c in report["missing_required_columns"]))
    if report["rows_missing_required"]:
        st.warning(f"⚠️ {report['rows_missing_required']} row(s) are missing one or more "
                   "required metadata fields (sample_id, date, fly_ash_type, NaOH_M, "
                   "time, temperature, L/S, CO2, initial/final pH).")
    if report["ph_out_of_range"]:
        st.warning("⚠️ pH outside 0–14: "
                   + "; ".join(f"row {p['row']} {p['column']}={p['value']:g}"
                               for p in report["ph_out_of_range"][:8]))
    if report["duplicate_sample_ids"]:
        st.warning("⚠️ Duplicate sample_id(s): "
                   + ", ".join(f"`{s}`" for s in report["duplicate_sample_ids"][:10]))
    if report["rows_no_measured_values"]:
        st.warning(f"⚠️ {report['rows_no_measured_values']} row(s) have no measured values "
                   "(no pH and no chemistry).")
    if report["converted_columns"]:
        st.info("Converted to mM: "
                + ", ".join(f"`{c}` (from {u})" for c, u in report["converted_columns"].items()))
    if report["classifications"]:
        st.caption("Row classification — "
                   + ", ".join(f"{k}: {v}" for k, v in report["classifications"].items()))

def _lab_data_import(run_name: str) -> None:
    """Data-tab import for a lab run — pick the import mode, then dispatch.

    Two modes share this run's ``experimental_release.csv``:
    a **generic** rectangular CSV/Excel importer, and a **special-case** parser for
    the Class C fly ash dissolution workbook (stacked ICP OES element blocks + a
    pH sheet). The generic importer is never removed.
    """
    st.markdown("**Upload experimental data file**")
    mode = st.radio(
        "Import mode",
        ["Generic table (CSV / Excel)", "Class C fly ash dissolution workbook"],
        key=f"lab_import_mode_{run_name}",
        help="Use the dissolution-workbook parser for the multi-block lab workbook "
             "(ICP OES element blocks + pH sheet); use the generic importer for a "
             "normal single-table CSV/Excel.",
    )
    if mode.startswith("Class C"):
        _dissolution_import(run_name)
    else:
        _generic_table_import(run_name)

    _render_model_predictions_import(run_name)

def _render_model_predictions_import(run_name: str) -> None:
    """"Import model predictions (CSV)" — a non-PHREEQC model via the generic contract.

    Same review-before-save pattern as the measured import. UI strings say *model
    predictions*; the model name is read from the file. Saved predictions become the
    manifest source (precedence over PHREEQC), so the same mapping/comparison applies.
    """
    from flyash_phreeqc_ml.parsers import generic_prediction_parser as gpp

    with st.expander("Import model predictions (CSV) — a model other than PHREEQC",
                     expanded=False):
        st.caption(
            "A non-PHREEQC model can supply predictions via the documented CSV contract "
            "(`docs/model_prediction_format.md`): required `record_key` + `model_name`, "
            "prediction columns `pred_pH` / `pred_Ca_mM` … in the profile's target units. "
            "These flow through the **same** manifest → mapping → comparison as PHREEQC."
        )
        up = st.file_uploader("Model predictions CSV", type=["csv"],
                              key=f"mp_up_{run_name}")
        if up is None:
            return
        try:
            raw = pd.read_csv(up)
        except Exception as exc:  # noqa: BLE001 - surface any read error to the user
            st.error(f"Could not read CSV: {exc}")
            return
        st.markdown("**Uploaded preview**")
        st.dataframe(raw.head(20), use_container_width=True, height=180)

        try:
            parsed = gpp.parse_predictions(raw)
        except gpp.PredictionContractError as exc:
            st.error(f"Contract error — fix the file and re-upload: {exc}")
            return

        model_names = sorted({str(m) for m in parsed["model_name"].unique()})
        st.success(f"Parsed {len(parsed)} prediction row(s) · model: "
                   f"{', '.join(model_names) or '(unnamed)'}.")
        manifest = scenarios.build_scenario_manifest(parsed)
        show_cols = [c for c in ("phreeqc_record_key", "scenario_label", "predicted_pH",
                                 "predicted_Ca_mM", "predicted_Si_mM", "predicted_Al_mM",
                                 "predicted_Fe_mM") if c in manifest.columns]
        st.markdown(f"**Model predictions to use ({', '.join(model_names) or 'model'}) — "
                    "review before save**")
        st.dataframe(manifest[show_cols], use_container_width=True, height=200,
                     hide_index=True)

        confirmed = st.checkbox(
            "I reviewed these model predictions and want to use them for mapping + "
            "comparison (they take precedence over PHREEQC).",
            key=f"mp_confirm_{run_name}")
        if st.button("Save model predictions", key=f"mp_save_{run_name}",
                     disabled=not confirmed):
            dest = config.PROCESSED_DIR / config.MODEL_PREDICTIONS_CSV
            dest.parent.mkdir(parents=True, exist_ok=True)
            parsed.to_csv(dest, index=False)
            audit.log_event(run_name, "model_predictions_import", {
                "file_name": getattr(up, "name", None),
                "model_names": model_names, "n_rows": int(len(parsed))})
            _scenario_manifest.clear()
            st.success(f"Saved {len(parsed)} model prediction(s) → "
                       f"`{_rel(dest)}`. The Match tab will use them.")
            st.rerun()

# --------------------------------------------------------------------------- #
# Optional AI import-assist (Data tab, generic importer) — suggestion-only.
# Everything it proposes flows into the existing review/confirm UI below; nothing
# AI-touched is saved without the explicit confirm-gated save.
# --------------------------------------------------------------------------- #
def _ai_sheet_previews(up, kind: str, raw: pd.DataFrame) -> list[dict]:
    """Build minimal per-sheet previews (headers + first rows) for classify_sheets."""
    n = import_assist.MAX_SAMPLE_ROWS
    if kind == "excel":
        data = up.getvalue()
        previews: list[dict] = []
        try:
            sheets = import_mapping.list_excel_sheets(io.BytesIO(data))
        except Exception:  # pragma: no cover - UI guard
            sheets = []
        for s in sheets:
            try:
                df = import_mapping.read_tabular(io.BytesIO(data), kind="excel", sheet=s)
            except Exception:
                continue
            previews.append({"sheet": s, "headers": [str(c) for c in df.columns],
                             "rows": df.head(n).astype(str).values.tolist()})
        return previews
    return [{"sheet": "csv", "headers": [str(c) for c in raw.columns],
             "rows": raw.head(n).astype(str).values.tolist()}]

def _ai_sample_id_source_column(run_name: str, raw: pd.DataFrame) -> str | None:
    """Which raw column feeds sample_id — AI suggestion first, else the fuzzy guess."""
    ai_colmap = st.session_state.get(f"ai_colmap_{run_name}") or []
    for c in ai_colmap:
        if c.get("target_col") == "sample_id" and c.get("source_col") in raw.columns:
            return c["source_col"]
    src = import_mapping.suggest_column_mapping(raw.columns).get("sample_id")
    return src if (src is not None and src in raw.columns) else None

def _render_ai_names_table(names: list[dict]) -> None:
    """Editable metadata-extraction table; each row badged rule / ai-suggested / unparsed."""
    badge = {import_assist.SOURCE_RULE: "rule", import_assist.SOURCE_AI: "ai-suggested"}
    rows = []
    for r in names:
        f = r.get("fields", {})
        rows.append({
            "sample_id": r.get("sample_id", ""),
            "provenance": badge.get(r.get("source"), "unparsed"),
            "confidence": r.get("confidence", 0.0),
            "leachant": f.get("leachant"), "concentration": f.get("concentration"),
            "condition_code": f.get("condition_code"), "time_min": f.get("time_min"),
            "replicate": f.get("replicate"), "note": r.get("note", ""),
        })
    st.markdown(
        app_ui.status_badge("rule", "exact") + " &nbsp; "
        + app_ui.status_badge("ai-suggested", "scenario-level") + " &nbsp; "
        + app_ui.status_badge("unparsed", "neutral"),
        unsafe_allow_html=True,
    )
    st.data_editor(pd.DataFrame(rows), use_container_width=True, height=240,
                   hide_index=True, key="ai_names_editor")
    st.caption("Suggestions only — saved rows keep their mapped values; the saved CSV "
               "records each row's provenance (`rule` / `ai-confirmed` / `manual`).")

def _render_ai_import_assist(run_name: str, up, kind: str, raw: pd.DataFrame) -> None:
    """The 'AI assist (optional)' expander: consent gate + a button per AI function.

    Suggestions land in session_state and are consumed by the existing column-mapping
    editor and the metadata-extraction table — nothing here saves or maps anything.
    """
    with st.expander("🤖 AI assist (optional) — propose interpretations of messy files"):
        if not import_assist.is_enabled():
            st.caption(
                "AI assist is disabled. Set the `ANTHROPIC_API_KEY` environment variable "
                "and `pip install anthropic` to enable optional suggestions. The importer "
                "works fully without it."
            )
            return

        st.caption(import_assist.DATA_LEAVES_MACHINE_NOTICE)
        consent = st.checkbox(import_assist.CONSENT_LABEL, key="ai_consent")
        if not consent:
            st.info("Tick the box above to allow sending headers + a small preview to the "
                    "API for these optional suggestions.")
            return

        headers = [str(c) for c in raw.columns]
        sample_rows = raw.head(import_assist.MAX_SAMPLE_ROWS).astype(str).values.tolist()
        b1, b2, b3 = st.columns(3)
        if b1.button("Classify sheets", key=f"ai_sheets_btn_{run_name}"):
            with st.spinner("Asking the model to classify sheets…"):
                st.session_state[f"ai_sheets_{run_name}"] = import_assist.classify_sheets(
                    _ai_sheet_previews(up, kind, raw))
        if b2.button("Suggest column mapping", key=f"ai_colmap_btn_{run_name}"):
            with st.spinner("Asking the model to map columns…"):
                st.session_state[f"ai_colmap_{run_name}"] = import_assist.propose_column_mapping(
                    headers, sample_rows, import_assist.default_target_schema())
            st.rerun()  # so the mapping editor below picks up the new defaults
        if b3.button("Extract sample-name fields", key=f"ai_names_btn_{run_name}"):
            src = _ai_sample_id_source_column(run_name, raw)
            ids = (raw[src].astype(str).tolist() if src else [])
            with st.spinner("Parsing sample names (rules first, AI for the rest)…"):
                st.session_state[f"ai_names_{run_name}"] = import_assist.parse_sample_names(
                    ids, profiles.default_dataset_profile())

        sheets = st.session_state.get(f"ai_sheets_{run_name}")
        if sheets:
            st.markdown("**Sheet classification (suggested)**")
            st.dataframe(pd.DataFrame(sheets), use_container_width=True, height=160,
                         hide_index=True)
        colmap = st.session_state.get(f"ai_colmap_{run_name}")
        if colmap:
            mapped = [c for c in colmap if c.get("target_col")]
            st.caption(f"🤖 {len(mapped)} column suggestion(s) applied as defaults in the "
                       "mapping editor below (badged *ai-suggested*).")
        names = st.session_state.get(f"ai_names_{run_name}")
        if names:
            st.markdown("**Sample-name fields (suggested)**")
            _render_ai_names_table(names)
        if st.button("Clear AI suggestions", key=f"ai_clear_{run_name}"):
            for k in (f"ai_sheets_{run_name}", f"ai_colmap_{run_name}", f"ai_names_{run_name}"):
                st.session_state.pop(k, None)
            st.rerun()

def _generic_table_import(run_name: str) -> None:
    """Generic CSV/Excel import into a lab run's experimental_release.csv.

    Reads .csv/.xlsx/.xls, previews the raw file, lets the user pick the Excel
    sheet, suggests a column mapping onto the app schema, converts chemistry units
    to mM, records leachant/provenance, validates, and only saves after explicit
    confirmation. Acid (HCl) rows are never forced into NaOH_M. Only ever writes to
    this lab run's own ``data/experimental_release.csv``.
    """
    st.caption(
        "Accepts `.csv`, `.xlsx`, or `.xls`. Preview → pick sheet → map columns → "
        "check units → confirm before saving to this run's `experimental_release.csv` "
        "(lab data only, never literature or synthetic)."
    )
    up = st.file_uploader(
        "Upload experimental data file", type=["csv", "xlsx", "xls"],
        key=f"lab_import_up_{run_name}",
    )
    if up is None:
        return

    raw, kind, sheet_name = _import_raw_frame(run_name, up)
    if raw is None:
        return

    # Feature 2 — raw preview before saving.
    st.markdown("**1 · Raw preview (nothing is saved yet)**")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("File", up.name)
    m2.metric("Type", kind)
    m3.metric("Sheet", sheet_name or "—")
    m4.metric("Raw shape", f"{raw.shape[0]} × {raw.shape[1]}")
    st.dataframe(raw, use_container_width=True, height=220)
    st.caption("Detected columns: " + ", ".join(f"`{c}`" for c in map(str, raw.columns)))

    # Optional AI assist — sets suggestion state consumed by the editors below.
    _render_ai_import_assist(run_name, up, kind, raw)

    # AI column-mapping suggestions (if any) become the editor defaults, layered
    # over the fuzzy guess. They are defaults only — the user still confirms.
    ai_colmap = st.session_state.get(f"ai_colmap_{run_name}") or []
    ai_target_to_source = {c["target_col"]: c["source_col"]
                           for c in ai_colmap if c.get("target_col")}
    ai_units = {c["source_col"]: c.get("unit_guess")
                for c in ai_colmap if c.get("unit_guess")}

    # Feature 3 — column mapping interface.
    st.markdown("**2 · Map uploaded columns → app schema**")
    st.caption("Suggestions are pre-filled from column names; adjust any of them. "
               "AI-suggested columns are badged below their selector.")
    suggestion = import_mapping.suggest_column_mapping(raw.columns)
    options = ["(leave blank)"] + [str(c) for c in raw.columns]
    mapping: dict[str, str | None] = {}
    mcols = st.columns(3)
    for i, target in enumerate(import_mapping.MAPPING_TARGETS):
        widget_col = mcols[i % 3]
        ai_default = ai_target_to_source.get(target)
        default = ai_default if (ai_default in options) else suggestion.get(target)
        idx = options.index(str(default)) if (default is not None and str(default) in options) else 0
        choice = widget_col.selectbox(target, options, index=idx, key=f"lab_map_{run_name}_{target}")
        if ai_default and ai_default in options:
            widget_col.caption("🤖 ai-suggested")
        mapping[target] = None if choice == "(leave blank)" else choice

    # Feature 4 — unit handling for mapped chemistry columns.
    st.markdown("**3 · Units for chemistry columns**")
    st.caption("mM = mg/L / atomic_mass. mg/L and ppm are equivalent; ppb = µg/L. "
               "Sc and total REE stay in ppb.")
    units: dict[str, str] = {}
    mapped_chem = [c for c in import_mapping.CHEM_VALUE_COLUMNS if mapping.get(c)]
    if mapped_chem:
        ucols = st.columns(3)
        for i, chem in enumerate(mapped_chem):
            ai_unit = ai_units.get(mapping.get(chem))
            uidx = (import_mapping.UNIT_OPTIONS.index(ai_unit)
                    if ai_unit in import_mapping.UNIT_OPTIONS else 0)
            units[chem] = ucols[i % 3].selectbox(
                f"{chem} unit", import_mapping.UNIT_OPTIONS, index=uidx,
                key=f"lab_unit_{run_name}_{chem}",
            )
    else:
        st.caption("No chemistry columns mapped — nothing to convert.")

    # Feature 5 — acid/base (leachant) support.
    st.markdown("**4 · Leachant (acid / base)**")
    default_leachant = st.selectbox(
        "Default leachant when not given in a column", ["NaOH", "HCl", "other"],
        key=f"lab_leachant_{run_name}",
    )
    st.caption(
        "A mapped `leachant` column overrides this per row. Rows that look like acid "
        "leaching get `NaOH_M` blanked, `leachant`/`acid_M` recorded, and a warning note."
    )

    # Feature 6/7 — build the transformed (schema-aligned) frame + validation.
    transformed = import_mapping.build_schema_frame(
        raw, mapping, units, filename=up.name, sheet_name=sheet_name,
        default_leachant=default_leachant,
    )
    st.markdown("**5 · Transformed preview & validation**")
    _import_render_report(import_mapping.summarize_import(transformed, units))
    st.dataframe(transformed, use_container_width=True, height=240)
    _render_unit_conversions_applied(transformed)

    if "sample_id" in transformed.columns:
        sid = transformed["sample_id"].astype(str).str.upper()
        flagged = int(sid.str.contains("TEST|SYNTH|MOCK|DEMO", na=False, regex=True).sum())
        if flagged:
            st.warning(f"⚠️ {flagged} sample_id(s) look like placeholders (TEST/SYNTH/MOCK/DEMO). "
                       "Only save real measured lab data into a lab run.")

    # Per-row metadata provenance: rule / ai-confirmed (AI-proposed and saved here) /
    # manual. Sourced from the sample-name extraction table; defaults to manual.
    names = st.session_state.get(f"ai_names_{run_name}") or []
    source_by_id = {str(r.get("sample_id")): r.get("source") for r in names}
    if "sample_id" in transformed.columns:
        sources = [source_by_id.get(str(sid), import_assist.SOURCE_MANUAL)
                   for sid in transformed["sample_id"].astype(str)]
    else:
        sources = [import_assist.SOURCE_MANUAL] * len(transformed)
    transformed[import_assist.METADATA_PROVENANCE_COLUMN] = (
        import_assist.build_provenance_column(sources))

    # Feature 8 — save options (replace / append), gated on confirmation.
    st.markdown("**6 · Save**")
    confirmed = st.checkbox(
        "I reviewed the imported data and understand these values will be saved to this "
        "experiment run.", key=f"lab_import_confirm_{run_name}",
    )
    existing = run_manager.read_data_file(run_name)

    def _do_save(mode: str) -> None:
        ctx = {"file_name": getattr(up, "name", None), "sheet": sheet_name or None,
               "column_mapping": {k: v for k, v in mapping.items() if v},
               "mapping_confirmed": True}
        dest = run_manager.save_lab_dataframe(run_name, transformed, mode=mode,
                                              audit_context=ctx)
        saved = run_manager.read_data_file(run_name)
        _read_csv.clear()
        st.success(f"Saved {len(transformed)} imported row(s) ({mode}) → "
                   f"`{_rel(dest)}`. Run now has {len(saved)} row(s).")
        st.dataframe(saved, use_container_width=True, height=240)

    if not existing.empty:
        st.info(f"This run already has {len(existing)} row(s). Choose how to save:")
        rc, ac = st.columns(2)
        if rc.button("Replace current run data", key=f"lab_import_replace_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")
        if ac.button("Append to current run data", key=f"lab_import_append_{run_name}",
                     disabled=not confirmed):
            _do_save("append")
    else:
        if st.button("Save imported data to this run", key=f"lab_import_save_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")

def _save_transformed(run_name: str, transformed: pd.DataFrame, key_prefix: str,
                      *, audit_context: dict | None = None) -> None:
    """Confirm-gated replace/append save of a transformed frame (shared by importers)."""
    confirmed = st.checkbox(
        "I reviewed the imported data and understand these values will be saved to this "
        "experiment run.", key=f"{key_prefix}_confirm_{run_name}",
    )
    existing = run_manager.read_data_file(run_name)

    def _do_save(mode: str) -> None:
        dest = run_manager.save_lab_dataframe(run_name, transformed, mode=mode,
                                              audit_context=audit_context)
        saved = run_manager.read_data_file(run_name)
        _read_csv.clear()
        st.success(f"Saved {len(transformed)} imported row(s) ({mode}) → "
                   f"`{_rel(dest)}`. Run now has {len(saved)} row(s).")
        st.dataframe(saved, use_container_width=True, height=240)

    if not existing.empty:
        st.info(f"This run already has {len(existing)} row(s). Choose how to save:")
        rc, ac = st.columns(2)
        if rc.button("Replace current run data", key=f"{key_prefix}_replace_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")
        if ac.button("Append to current run data", key=f"{key_prefix}_append_{run_name}",
                     disabled=not confirmed):
            _do_save("append")
    else:
        if st.button("Save imported data to this run", key=f"{key_prefix}_save_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")

def _dissolution_import(run_name: str) -> None:
    """Special-case importer for the Class C fly ash dissolution workbook.

    Parses the stacked ICP OES element blocks + pH sheet via
    :mod:`dissolution_workbook`, lets the user set shared metadata defaults and the
    NaOH/HCl scope, shows a normalised preview with parse counts and warnings, and
    saves (confirm-gated) into this lab run's ``experimental_release.csv``. The
    generic importer remains available via the mode selector above.
    """
    st.caption(
        "For the multi-block lab workbook: an **ICP OES** sheet (Calcium / Silicon / "
        "Aluminum blocks, columns NaOH-OA/PF/GS, mmol/l + mg/L) and a **pH** sheet "
        "(`0.5M NaOH-OA-10`-style rows). mmol/l is preferred; mg/L is converted to mM."
    )
    up = st.file_uploader(
        "Upload dissolution workbook (.xlsx / .xls)", type=["xlsx", "xls"],
        key=f"diss_up_{run_name}",
    )
    if up is None:
        return

    # Feature 6 — shared metadata the user sets once for every imported row.
    st.markdown("**1 · Default metadata for all imported rows**")
    st.caption("These are not in the workbook — set them once and they fill every row. "
               "Rows are not rejected just because these are blank.")
    d1, d2, d3 = st.columns(3)
    defaults = {
        "experiment_date": d1.text_input("experiment_date", key=f"diss_date_{run_name}"),
        "temperature_C": d2.text_input("temperature_C", key=f"diss_temp_{run_name}"),
        "liquid_solid_ratio": d3.text_input("liquid_solid_ratio", key=f"diss_ls_{run_name}"),
        "CO2_condition": d1.selectbox("CO2_condition", _CO2_OPTIONS, key=f"diss_co2_{run_name}"),
        "initial_pH": d2.text_input("initial_pH", key=f"diss_iph_{run_name}"),
        "fly_ash_type": d3.text_input("fly_ash_type", value=dissolution_workbook.FLY_ASH_DEFAULT,
                                      key=f"diss_fat_{run_name}"),
    }

    # Feature 10 — NaOH-only vs NaOH+HCl.
    st.markdown("**2 · Rows to import**")
    scope = st.radio(
        "Scope", ["Import only NaOH rows", "Import NaOH + HCl rows"],
        index=1, key=f"diss_scope_{run_name}",
    )
    include_hcl = scope.endswith("HCl rows")

    try:
        transformed, report = dissolution_workbook.normalize_dissolution_workbook(
            io.BytesIO(up.getvalue()), defaults=defaults, include_hcl=include_hcl,
            filename=up.name,
        )
    except dissolution_workbook.DissolutionWorkbookError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # pragma: no cover - UI guard
        st.error(f"Could not parse workbook: {exc}")
        return

    if transformed.empty:
        st.warning("No NaOH/HCl sample rows were parsed from the pH sheet — check the workbook "
                   "matches the expected structure.")
        return

    # Feature 8 — normalised preview + counts.
    st.markdown("**3 · Normalised preview (nothing is saved yet)**")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("NaOH rows", report["n_naoh"])
    c2.metric("HCl rows", report["n_hcl"])
    c3.metric("Rows with pH", report["n_with_ph"])
    c4.metric("Rows with Ca/Si/Al", report["n_with_chem"])
    c5.metric("Rows missing metadata", report["rows_missing_metadata"])

    # Feature 9 — warnings.
    for msg in report["warnings"]:
        st.warning("⚠️ " + msg)

    st.dataframe(transformed, use_container_width=True, height=300)

    # Feature 8 — debug view: detected ICP tables + joined chemistry.
    with st.expander("Debug — detected ICP mmol/l tables & joined chemistry", expanded=False):
        debug = report.get("icp_debug", {})
        if not debug:
            st.info("No ICP chemistry was detected. Check the ICP OES sheet has element block "
                    "labels (Calcium/Silicon/Aluminum), a `mg/L` and `mmol/l` header row, and "
                    "`NaOH-OA/PF/GS` columns.")
        else:
            st.caption("Each table is the value used per (time, condition) — mmol/l preferred, "
                       "mg/L converted to mM as a fallback.")
            for col, label in (("Ca_mM", "Calcium"), ("Si_mM", "Silicon"), ("Al_mM", "Aluminum")):
                if col in debug:
                    st.markdown(f"**{label} → `{col}`**")
                    st.dataframe(debug[col], use_container_width=True, height=180)
        st.markdown("**Joined normalised rows (chemistry columns)**")
        st.dataframe(
            transformed[["sample_id", "time_min", "extra__condition_code", "final_pH",
                         "Ca_mM", "Si_mM", "Al_mM"]],
            use_container_width=True, height=240,
        )

    # Save (confirm-gated).
    st.markdown("**4 · Save**")
    _save_transformed(run_name, transformed, key_prefix="diss",
                      audit_context={"file_name": getattr(up, "name", None),
                                     "sheet": "dissolution_workbook"})

def _lab_entry_form(run_name: str) -> None:
    """Measured-release entry form for a lab-type run (pH-only or full ICP)."""
    st.write(
        "Enter a measured-release row. **Leave any chemistry field blank if not "
        "measured** — pH-only rows are fine; add ICP numbers later."
    )
    with st.form(f"lab_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
            widget_col = cols[i % 3]
            numeric = column in config.EXPERIMENTAL_NUMERIC_COLUMNS
            label = f"{column} (number)" if numeric else column
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"{run_name}_{column}")
            elif column == "precipitate_observed":
                inputs[column] = widget_col.selectbox(column, _YESNO_OPTIONS, key=f"{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(label, value="", key=f"{run_name}_{column}")
        submitted = st.form_submit_button("Save row to this run")

    if submitted:
        errors: list[str] = []
        for column in config.EXPERIMENTAL_NUMERIC_COLUMNS:
            raw = (inputs.get(column) or "").strip()
            if raw and _is_not_number(raw):
                errors.append(f"'{column}' must be a number (got '{raw}').")
        if not (inputs.get("sample_id") or "").strip():
            errors.append("'sample_id' is required.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            row = {c: (inputs.get(c) or "").strip() for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
            path = run_manager.append_lab_row(run_name, row)
            st.success(f"Saved sample '{row['sample_id']}' to {_rel(path)}.")
            _read_csv.clear()

def _literature_entry(run_name: str) -> None:
    """Manual row entry + CSV upload for a literature-benchmark run."""
    st.write(
        "**Literature benchmark data** — values reported by other papers, for "
        "comparison only. This is kept separate from our measured experiment and is "
        "never written to a lab run's `experimental_release.csv`."
    )
    up = st.file_uploader("Upload a literature CSV", type=["csv"], key=f"lit_up_{run_name}")
    if up is not None:
        try:
            df = pd.read_csv(up)
            path = run_manager.save_literature_dataframe(run_name, df)
            st.success(f"Saved {len(df)} row(s) to {_rel(path)}.")
            _read_csv.clear()
        except Exception as exc:  # pragma: no cover - UI guard
            st.error(f"Could not read CSV: {exc}")

    with st.form(f"lit_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(run_manager.LITERATURE_BENCHMARK_COLUMNS):
            widget_col = cols[i % 3]
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"lit_{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(column, value="", key=f"lit_{run_name}_{column}")
        submitted = st.form_submit_button("Add literature row")
    if submitted:
        if not (inputs.get("source_id") or "").strip():
            st.error("'source_id' is required for a literature row.")
        else:
            row = {c: (inputs.get(c) or "").strip() for c in run_manager.LITERATURE_BENCHMARK_COLUMNS}
            path = run_manager.append_literature_row(run_name, row)
            st.success(f"Added literature row '{row['source_id']}' to {_rel(path)}.")
            _read_csv.clear()

def _demo_entry(run_name: str) -> None:
    """Add synthetic demo rows (every row tagged source_type=synthetic_demo)."""
    st.error(
        "🧩 This is **synthetic / demo data only** — for testing the code, not for "
        "scientific conclusions. Every row is tagged `source_type=synthetic_demo`."
    )
    with st.form(f"demo_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
            widget_col = cols[i % 3]
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"demo_{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(column, value="", key=f"demo_{run_name}_{column}")
        submitted = st.form_submit_button("Add demo row")
    if submitted:
        if not (inputs.get("sample_id") or "").strip():
            st.error("'sample_id' is required.")
        else:
            row = {c: (inputs.get(c) or "").strip() for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
            path = run_manager.append_demo_row(run_name, row)
            st.success(f"Added demo row '{row['sample_id']}' to {_rel(path)}.")
            _read_csv.clear()

def _is_not_number(raw: str) -> bool:
    try:
        float(raw)
        return False
    except ValueError:
        return True

def _render_unit_conversions_applied(df: pd.DataFrame) -> None:
    """"Unit conversions applied" expander: original→target, formula, molar mass, examples.

    Reads the conversion-provenance companions an import wrote, so every converted
    value is traceable to its original value + unit + the registry conversion id.
    Renders nothing when the frame carries no converted columns.
    """
    rows = import_mapping.conversion_provenance_summary(df)
    if not rows:
        return
    with st.expander("Unit conversions applied", expanded=False):
        st.caption("Every converted column kept its original value, original unit, and the "
                   "registry `conversion_id`, so the conversion is auditable later. "
                   f"Molar masses: {units.MOLAR_MASS_SOURCE}.")
        for r in rows:
            mm = r["molar_mass_g_mol"]
            head = (f"**{r['column']}** — {r['from_unit']} → {r['to_unit']}  ·  "
                    f"`{r['conversion_id']}`")
            if mm is not None:
                head += f"  ·  M_{r['element']} = {mm:g} g/mol"
            st.markdown(head)
            st.caption(f"formula: `{r['formula']}`")
            if r["examples"]:
                st.dataframe(pd.DataFrame(r["examples"]), use_container_width=True,
                             hide_index=True, height=min(40 + 35 * len(r["examples"]), 160))

def _render_run_data_and_edit(run_name: str, rt: str) -> None:
    """This run's data table + row deletion + CSV/pipeline export (no mapping)."""
    data = run_manager.read_data_file(run_name)
    st.markdown(f"**This run's data** ({len(data)} row(s)):")
    st.dataframe(data, use_container_width=True, height=300)
    _render_unit_conversions_applied(data)

    # --- Delete / clean rows --- only affects THIS run's CSV.
    if not data.empty:
        with st.expander("🗑️ Delete rows", expanded=False):
            id_col = run_manager.id_column_for(run_name)

            def _row_label(i: int) -> str:
                if id_col in data.columns:
                    val = data.iloc[i][id_col]
                    shown = "" if pd.isna(val) else str(val).strip()
                    return f"Row {i} — {id_col}={shown or '(blank)'}"
                return f"Row {i}"

            to_delete = st.multiselect(
                "Select row numbers to delete",
                options=list(range(len(data))),
                format_func=_row_label,
                key=f"del_rows_{run_name}",
            )
            confirm = st.checkbox(
                "I understand this will delete the selected rows from this run's CSV.",
                key=f"del_confirm_{run_name}",
            )
            if st.button("Delete selected rows", key=f"del_btn_{run_name}"):
                if not to_delete:
                    st.warning("No rows selected — nothing was deleted.")
                elif not confirm:
                    st.warning("Tick the confirmation checkbox before deleting.")
                else:
                    n = run_manager.delete_data_rows(run_name, to_delete)
                    st.success(f"Deleted {n} row(s) from this run's CSV.")
                    _read_csv.clear()
                    st.rerun()

            st.divider()
            st.caption(f"Remove rows with a blank `{id_col}` or where every value is empty.")
            if st.button("Remove blank rows", key=f"del_blank_{run_name}"):
                n = run_manager.remove_blank_data_rows(run_name)
                if n:
                    st.success(f"Removed {n} blank row(s).")
                    _read_csv.clear()
                    st.rerun()
                else:
                    st.info("No blank rows found.")

    # Export this run's CSV.
    ec1, ec2 = st.columns(2)
    with ec1:
        if not data.empty:
            export_name = f"{run_name}_{run_manager.spec_for(rt).data_filename}"
            if st.download_button(
                "⬇️ Export this run's CSV",
                data=data.to_csv(index=False).encode("utf-8"),
                file_name=export_name,
                mime="text/csv",
                use_container_width=True,
            ):
                audit.log_export(run_name, kind="run_csv", file_name=export_name,
                                 n_rows=int(len(data)))
    with ec2:
        if rt in run_manager.LAB_LIKE_RUN_TYPES:
            if st.button("➡️ Export to pipeline (manual-entry CSV)", use_container_width=True,
                         key=f"export_pipe_{run_name}"):
                try:
                    dest = run_manager.export_lab_run_to_pipeline(run_name)
                    audit.log_export(run_name, kind="pipeline_manual_entry",
                                     file_name=dest.name, n_rows=int(len(data)))
                    st.success(
                        f"Copied to {_rel(dest)} — the existing "
                        "scripts (05/07) will pick it up."
                    )
                    _read_csv.clear()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))

def _render_import_tab(selected_run: str | None) -> None:
    app_ui.render_page_header(
        "Import — add experimental data",
        "Import or enter your experimental (measured) data for the selected run, review the "
        "detected records and unit conversions, then save to the run. Measured data is used "
        "later to validate and correct simulated predictions.",
        eyebrow="Validation module · Import",
    )
    _render_next_step(selected_run)
    app_ui.render_workflow_steps(
        ["Upload / import data", "Review detected records", "Review units", "Save to run"],
        current=0,
    )
    if not selected_run:
        st.info("Select or create a run in the **Experiment runs** sidebar (left) to import "
                "data. Use **lab_experiment** for measured ICP/pH data, **literature_benchmark** "
                "for reported values, or **synthetic_demo** for testing.")
        return
    cfg = run_manager.load_run_config(selected_run)
    rt = cfg.get("run_type")
    app_ui.section_header(f"Run · {selected_run}", rt)
    _run_type_warning(rt)

    if rt in run_manager.LAB_LIKE_RUN_TYPES:
        _lab_data_import(selected_run)
        st.divider()
        _lab_entry_form(selected_run)
    elif rt == "literature_benchmark":
        _literature_entry(selected_run)
    elif rt == "synthetic_demo":
        _demo_entry(selected_run)

    st.divider()
    _render_run_data_and_edit(selected_run, rt)
    st.caption("Data-quality validation moved to the **Validate** tab.")

    st.divider()
    with st.expander("Legacy global data entry — not recommended", expanded=False):
        st.caption(
            "This form predates per-run save files and writes to one shared "
            "pipeline file. Prefer the run-specific entry above."
        )
        _render_legacy_global_form()

def _render_legacy_global_form() -> None:
    st.write(
        f"Submitting appends one row to `{_rel(MANUAL_ENTRY_PATH)}` "
        "(existing rows are never overwritten). Leave a field blank if not measured."
    )
    with st.form("experimental_entry", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
            widget_col = cols[i % 3]
            numeric = column in config.EXPERIMENTAL_NUMERIC_COLUMNS
            label = f"{column} (number)" if numeric else column
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS)
            elif column == "precipitate_observed":
                inputs[column] = widget_col.selectbox(column, _YESNO_OPTIONS)
            else:
                inputs[column] = widget_col.text_input(label, value="")
        submitted = st.form_submit_button("Save row")

    if submitted:
        errors: list[str] = []
        for column in config.EXPERIMENTAL_NUMERIC_COLUMNS:
            raw = (inputs.get(column) or "").strip()
            if raw == "":
                continue
            try:
                float(raw)
            except ValueError:
                errors.append(f"'{column}' must be a number (got '{raw}').")
        if not inputs.get("sample_id", "").strip():
            errors.append("'sample_id' is required.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            row = {col: (inputs.get(col) or "").strip() for col in config.EXPERIMENTAL_RELEASE_COLUMNS}
            new_df = pd.DataFrame([row], columns=config.EXPERIMENTAL_RELEASE_COLUMNS)
            MANUAL_ENTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            write_header = not MANUAL_ENTRY_PATH.exists()
            new_df.to_csv(MANUAL_ENTRY_PATH, mode="a", header=write_header, index=False)
            total = len(pd.read_csv(MANUAL_ENTRY_PATH))
            st.success(
                f"Saved sample '{row['sample_id']}'. "
                f"{MANUAL_ENTRY_PATH.name} now has {total} row(s)."
            )
            st.dataframe(new_df, use_container_width=True)
            _read_csv.clear()

    if MANUAL_ENTRY_PATH.exists():
        existing = pd.read_csv(MANUAL_ENTRY_PATH)
        st.markdown("**Current manual-entry file:**")
        st.dataframe(existing, use_container_width=True, height=300)


# Tab entry point (app.py calls ui.import_tab.render).
render = _render_import_tab
