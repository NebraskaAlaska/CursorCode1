"""Match tab — measured <-> model-prediction mapping (suggestions + manual).

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import app_ui  # noqa: E402  (presentation-only UI helper layer)
from ui.formatters import is_present as _is_present  # noqa: E402
from flyash_phreeqc_ml import audit  # noqa: E402  (append-only audit log)
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import mapping_table  # noqa: E402
from flyash_phreeqc_ml import phreeqc_runner  # noqa: E402  (on-demand PHREEQC, Prompt 11)
from flyash_phreeqc_ml import profiles  # noqa: E402
from flyash_phreeqc_ml import replicates  # noqa: E402
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml import scenarios  # noqa: E402

from ui.common import _audit_once, _render_next_step
from ui.state import MODEL_NAME, _read_csv, _rel, _scenario_manifest

def _render_mapping_quality(mapping: pd.DataFrame) -> None:
    """Small mapping-quality summary + collision warning for the Mapping tab."""
    summary = run_manager.summarize_mapping(mapping)
    if summary["n_samples"] == 0:
        return

    st.markdown("**Mapping quality**")
    m1, m2 = st.columns(2)
    m1.metric("Mapped samples", summary["n_samples"])
    m2.metric("Unique model results used", summary["n_unique_rows"])

    if summary["samples_per_row"]:
        per_row = pd.DataFrame(
            [{"model_result": k, "samples_mapped": v}
             for k, v in summary["samples_per_row"].items()]
        )
        st.caption("Samples per model result:")
        st.dataframe(per_row, use_container_width=True, height=170)

    if summary["has_collisions"]:
        st.warning(
            "Multiple samples are mapped to the same model result. Scatter plots may "
            "appear as vertical lines because the model prediction is identical for "
            "those samples.\n\n"
            "Your graph may form a vertical line because several samples share the "
            "same model prediction."
        )
    if summary["n_samples"] > summary["n_unique_rows"]:
        st.warning(
            "There are more samples than distinct model results, so the comparison may "
            "not represent distinct model conditions."
        )

# Columns the Scenario Explorer table shows (readable subset of the manifest).
_EXPLORER_COLUMNS = [
    "scenario_label", "source_file", "state", "solution_number",
    "predicted_pH", "predicted_Ca_mM", "predicted_Si_mM", "predicted_Al_mM",
    "predicted_Fe_mM", "liquid_solid_ratio", "CO2_condition", "metadata_quality",
]

def _render_scenario_explorer(run_name: str, manifest: pd.DataFrame) -> None:
    """Feature 2 — filterable, readable table of PHREEQC scenarios."""
    st.markdown("#### PHREEQC Scenario Explorer")
    st.caption(
        "Each PHREEQC result row described in plain terms, with metadata inferred "
        "from the source filename where safe (anything uncertain is `unknown`)."
    )
    if manifest.empty:
        st.info("No PHREEQC scenarios yet — run Phase 1 to generate results.")
        return

    f1, f2, f3, f4 = st.columns(4)
    state_choice = f1.selectbox(
        "state", ["batch only", "initial only", "all"], key=f"exp_state_{run_name}")
    co2_opts = ["all"] + sorted(manifest["CO2_condition"].dropna().astype(str).unique())
    co2_choice = f2.selectbox("CO2_condition", co2_opts, key=f"exp_co2_{run_name}")
    ls_vals = sorted(
        {f"{v:g}" for v in manifest["liquid_solid_ratio"].dropna().tolist()})
    ls_choice = f3.selectbox("liquid_solid_ratio", ["all"] + ls_vals, key=f"exp_ls_{run_name}")
    src_opts = ["all"] + sorted(manifest["source_file"].dropna().astype(str).unique())
    src_choice = f4.selectbox("source_file", src_opts, key=f"exp_src_{run_name}")

    view = manifest
    if state_choice == "batch only":
        view = view[view["state"].astype(str).str.lower() == "batch"]
    elif state_choice == "initial only":
        view = view[view["state"].astype(str).str.lower() == "initial"]
    if co2_choice != "all":
        view = view[view["CO2_condition"].astype(str) == co2_choice]
    if ls_choice != "all":
        view = view[view["liquid_solid_ratio"].map(lambda v: f"{v:g}") == ls_choice]
    if src_choice != "all":
        view = view[view["source_file"].astype(str) == src_choice]

    cols = [c for c in _EXPLORER_COLUMNS if c in view.columns]
    st.dataframe(view[cols], use_container_width=True, height=280)
    st.caption(f"{len(view)} of {len(manifest)} scenarios shown.")

    st.info(
        "**sol1, sol2, sol3 are PHREEQC solution numbers / repeated solution outputs.** They "
        "should not be interpreted as experimental time points unless the PHREEQC input "
        "explicitly defines them that way. **In this project, sol1/sol2/sol3 should be treated "
        "as replicate/batch outputs for the same broad scenario, not as 10/20/60 min time points.**"
    )
    descriptions = scenarios.load_solution_descriptions()
    with st.expander("What do sol1 / sol2 / sol3 represent? (from the PHREEQC input files)"):
        if descriptions.empty:
            st.caption(
                "No parsed PHREEQC input solutions found yet (run Phase 1). PHREEQC `.pqi` "
                "files label each `SOLUTION n`; those labels — not time or replicate — are "
                "all the app can attribute to a solution number."
            )
        else:
            st.caption(
                "Parsed from the `.pqi` input files (`SOLUTION n <label>`). A blank/generic "
                "label means the input did not describe that solution as a time point or replicate."
            )
            st.dataframe(descriptions, use_container_width=True, height=200)

_ASSISTANT_META_FIELDS = [
    "NaOH_M", "time_min", "liquid_solid_ratio", "CO2_condition",
    "temperature_C", "final_pH", "Ca_mM", "Si_mM", "Al_mM", "Fe_mM",
]

def _render_mapping_assistant(run_name: str, data: pd.DataFrame,
                              sample_ids: list[str], manifest: pd.DataFrame) -> None:
    """Features 3/4/5 — sample metadata, top-3 suggestions, approve, no-match warning."""
    st.markdown("#### Mapping Assistant")
    st.caption(
        "Pick a measured sample; the assistant scores PHREEQC scenarios with simple, "
        "transparent rules (no ML) and suggests the best matches."
    )
    sel_sample = st.selectbox("Experimental sample_id", sample_ids,
                              key=f"assist_sample_{run_name}")
    sample_row = data[data["sample_id"].astype(str).str.strip() == sel_sample]
    sample = sample_row.iloc[0].to_dict() if not sample_row.empty else {"sample_id": sel_sample}

    meta = {f: sample.get(f, "") for f in _ASSISTANT_META_FIELDS if f in sample}
    exp_code = scenarios.sample_condition_code(sample)
    if exp_code:
        meta["condition_code"] = exp_code
        meta["cover_condition"] = scenarios.cover_condition(exp_code) or ""
        meta["CO2_exposure_level"] = scenarios.co2_exposure_level(exp_code) or ""
    if meta:
        st.markdown("**Measured metadata (known):**")
        st.dataframe(pd.DataFrame([meta]), use_container_width=True, height=80)
        st.caption(
            "The model (currently PHREEQC) knows L/S, CO2, state, solution number and "
            "predicted pH/Ca/Si/Al — but often **not** fine-grained measured metadata such "
            "as exact time, condition code, or concentration, so an exact match can't always "
            "be confirmed (see notes below)."
        )

    if manifest.empty:
        st.info("No PHREEQC scenarios available to suggest yet — run Phase 1 first.")
        return

    suggestions = scenarios.suggest_mappings(sample, manifest, top_n=3)
    if not suggestions or suggestions[0]["confidence"] == "low":
        # Feature 5 — best match is weak.
        st.error(
            "No strong PHREEQC match exists for this sample. The comparison may be "
            "misleading. Consider generating a new PHREEQC simulation for this "
            "experimental condition."
        )

    for i, sug in enumerate(suggestions):
        conf = sug["confidence"]
        badge = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}.get(conf, conf)
        with st.container(border=True):
            st.markdown(f"**{sug['scenario_label']}** · score {sug['score']} · {badge} confidence")
            st.caption(f"`{sug['suggested_phreeqc_record_key']}`")
            if sug.get("base_confidence") and sug["base_confidence"] != conf:
                st.caption(
                    f"Capped from {sug['base_confidence']} → {conf}: PHREEQC does not specify "
                    f"{', '.join(sug.get('phreeqc_missing', [])) or 'some experimental metadata'}."
                )
            st.markdown(
                f"- **Reason:** {sug['reason']}\n"
                f"- **Matched:** {', '.join(sug['matched_fields']) or '—'}\n"
                f"- **Mismatched:** {', '.join(sug['mismatched_fields']) or '—'}\n"
                f"- **Missing PHREEQC metadata:** {', '.join(sug.get('phreeqc_missing', [])) or '—'}"
            )
            for note in sug.get("metadata_notes", []):
                st.warning("ℹ️ " + note)
            if st.button("Use this mapping", key=f"assist_use_{run_name}_{i}"):
                try:
                    run_manager.add_mapping(
                        run_name, sel_sample, sug["suggested_phreeqc_record_key"])
                    st.success(
                        f"Mapped `{sel_sample}` → `{sug['suggested_phreeqc_record_key']}`.")
                    _read_csv.clear()
                    st.rerun()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))

def _render_generate_simulation(run_name: str, data: pd.DataFrame,
                                manifest: pd.DataFrame, needs_new: pd.DataFrame) -> None:
    """Generate (and optionally run) a PHREEQC simulation for a needs-new condition.

    Preview-first: the templated ``.pqi`` and its chemistry-assumptions banner are
    always shown (no PHREEQC needed to read them). Running + ingesting requires a
    configured PHREEQC; afterwards the suggestion table refreshes and the new
    scenario can be mapped (OA → exact; PF/GS → scenario-level by design).
    """
    app_ui.section_header("Generate a PHREEQC simulation",
                          "make a needs-new condition actionable")
    configured = phreeqc_runner.is_configured()
    if not configured:
        st.info("PHREEQC is not configured, so simulations can be **previewed** here but not "
                "run. Set `PHREEQC_EXE` + `PHREEQC_DATABASE` to enable running. The generated "
                "input below is still fully readable.")

    profile = profiles.default_dataset_profile()
    ck = st.selectbox("Condition needing a new simulation",
                      needs_new["condition_key"].astype(str).tolist(),
                      key=f"gen_ck_{run_name}")
    if st.button("Generate simulation input", key=f"gen_btn_{run_name}"):
        sample = mapping_table.condition_representative_sample(data, ck, profile)
        reason = phreeqc_runner.generation_blocked_reason(sample, profile)
        if reason:
            st.warning("⚠️ " + reason)
            st.session_state.pop(f"gen_inputs_{run_name}", None)
        else:
            st.session_state[f"gen_inputs_{run_name}"] = {
                "ck": ck, "inputs": phreeqc_runner.build_input(sample, profile)}

    payload = st.session_state.get(f"gen_inputs_{run_name}")
    if not payload or payload.get("ck") != ck:
        return
    inputs = payload["inputs"]
    if not inputs:
        st.info("No simulation can be templated for this condition.")
        return

    for gi in inputs:
        with st.expander(f"Generated input — {gi.model_label} ({gi.condition_code})",
                         expanded=True):
            app_ui.render_warning_panel(
                "Chemistry assumptions — read before running",
                "; ".join(gi.assumptions), level="warning")
            st.code(gi.pqi_text, language="text")

    if not configured:
        app_ui.render_warning_panel("PHREEQC not configured", phreeqc_runner._SETUP_HELP,
                                    level="error")
        return

    if st.button(f"▶️ Run PHREEQC for {len(inputs)} variant(s) & ingest",
                 key=f"gen_run_{run_name}", type="primary"):
        workdir = run_manager.generated_simulations_dir(run_name)
        ok = 0
        for gi in inputs:
            try:
                out = phreeqc_runner.run(gi.pqi_text, workdir, basename=gi.basename)
                keys = phreeqc_runner.ingest(out, run_name,
                                             condition_key=gi.source_condition_key,
                                             metadata=gi.metadata)
                st.write(f"✅ **{gi.model_label}** — {len(keys)} record(s) ingested")
                ok += 1
            except phreeqc_runner.PhreeqcRunnerError as exc:
                st.write(f"❌ **{gi.model_label}** — {str(exc).splitlines()[0]}")
        if ok:
            st.success(f"Ingested {ok} generated scenario(s) into the shared results — "
                       "the suggestion table will refresh with the new candidate(s).")
            _read_csv.clear()
            _scenario_manifest.clear()
            st.session_state.pop(f"gen_inputs_{run_name}", None)
            st.rerun()

def _render_sim_needed(run_name: str, data: pd.DataFrame, mapping: pd.DataFrame,
                       manifest: pd.DataFrame, table: pd.DataFrame) -> None:
    """Conditions whose best candidate is *needs new simulation*.

    Driven from the **same** suggestion ``table`` shown above, so the count here
    always agrees with the table's status column.
    """
    st.markdown(f"#### Conditions needing new {MODEL_NAME} simulations")
    needs_new = mapping_table.needs_new_simulation(table)
    st.caption(
        "Conditions with no usable model/simulation candidate (status **needs new "
        f"simulation** in the suggestion table). Generate matching {MODEL_NAME} scenarios for "
        "these. Conditions that are *scenario-level only* or *unsafe* appear in the table "
        "above with that status, not here."
    )
    if needs_new.empty:
        st.success("Every measured condition has at least one candidate model scenario.")
    else:
        st.dataframe(needs_new[["condition_key", "n_replicates", "confidence", "reason"]],
                     use_container_width=True, height=240)
        # Make it actionable: generate a PHREEQC input for one of these conditions.
        _render_generate_simulation(run_name, data, manifest, needs_new)

    # Keep the older per-sample view available but out of the way.
    with st.expander("Per-sample detail (no mapping / low confidence / collisions)"):
        needed = scenarios.samples_needing_simulation(data, mapping, manifest)
        if needed.empty:
            st.success("Every sample has a confident, non-colliding PHREEQC mapping.")
        else:
            st.dataframe(needed, use_container_width=True, height=240)

def _render_manual_mapping(run_name: str, phreeqc: pd.DataFrame,
                           sample_ids: list[str]) -> None:
    """Feature 7 — the original manual dropdown, kept as advanced mode."""
    st.warning(
        "Use manual mapping only if you understand what the PHREEQC row represents."
    )
    only_batch = st.checkbox(
        "Only show PHREEQC 'batch' rows (post-equilibration)",
        value=True, key=f"map_batch_{run_name}",
    )
    view = phreeqc
    if only_batch and "state" in phreeqc.columns:
        batch = phreeqc[phreeqc["state"] == "batch"]
        view = batch if not batch.empty else phreeqc

    label_cols = [
        c for c in ["record_key", "source_file", "simulation", "state",
                    "solution_number", "pH", "mol_Ca", "mol_Si", "mol_Al", "mol_Na"]
        if c in view.columns
    ]

    def _phreeqc_label(pos) -> str:
        row = view.loc[pos]
        return " | ".join(f"{c}={row[c]}" for c in label_cols)

    c1, c2 = st.columns(2)
    with c1:
        sel_sample = st.selectbox("sample_id", sample_ids, key=f"map_sample_{run_name}")
    with c2:
        sel_pos = st.selectbox(
            "PHREEQC result row", list(view.index),
            format_func=_phreeqc_label, key=f"map_pheq_{run_name}",
        )

    if st.button("Save mapping", key=f"map_save_{run_name}"):
        record_key = str(view.loc[sel_pos, "record_key"]).strip()
        try:
            run_manager.add_mapping(run_name, sel_sample, record_key)
            st.success(f"Mapped `{sel_sample}` → `{record_key}`.")
            _read_csv.clear()
            st.rerun()
        except run_manager.RunManagerError as exc:
            st.error(str(exc))

def _render_replicate_summary(data: pd.DataFrame) -> None:
    """Feature 2 — replicate count + mean/std per experimental condition."""
    st.markdown("#### Replicate summary (grouped by condition)")
    st.caption(
        "Rows are grouped by `condition_key` (leachant + molarity + OA/PF/GS + time + "
        "L/S + CO2). `replicate_id` is read from the sample_id (R1/R2/R3, rep…, batch…). "
        "std needs ≥2 replicates."
    )
    summary = replicates.replicate_summary(data)
    if summary.empty:
        st.info("No rows to summarise yet.")
        return
    st.dataframe(summary, use_container_width=True, height=220)
    singles = summary[summary["number_of_replicates"] < 2]
    if not singles.empty:
        st.warning(f"⚠️ {len(singles)} condition(s) have a single replicate — no standard "
                   "deviation can be estimated for them.")

def _scenario_record_key_picker(run_name: str, manifest: pd.DataFrame, key: str) -> str | None:
    """Selectbox over batch-first manifest scenarios -> a PHREEQC record_key."""
    if manifest.empty:
        st.info("No PHREEQC scenarios yet — run Phase 1 first.")
        return None
    view = manifest.copy()
    view["_batch"] = view["state"].astype(str).str.lower().eq("batch")
    view = view.sort_values("_batch", ascending=False)
    keys = view["phreeqc_record_key"].astype(str).tolist()
    labels = {
        r["phreeqc_record_key"]: f"{r.get('scenario_label', '')}  ·  {r['phreeqc_record_key']}"
        for _, r in view.iterrows()
    }
    return st.selectbox("Model / simulation result", keys,
                        format_func=lambda k: labels.get(k, k), key=key,
                        help="Currently PHREEQC scenario rows. Each label includes its "
                             "PHREEQC file/source and solution number.")

# Status → (emoji, Streamlit alert level) for the simple mapping-status line.
_MAPPING_STATUS_BADGE = {
    replicates.MAPPING_STATUS_EXACT: ("✅", "success"),
    replicates.MAPPING_STATUS_SCENARIO: ("🟡", "warning"),
    replicates.MAPPING_STATUS_UNSAFE: ("🔴", "error"),
    replicates.MAPPING_STATUS_NEEDS_NEW: ("⚠️", "warning"),
}

def _manifest_row(manifest: pd.DataFrame, record_key: str | None) -> dict | None:
    """The manifest row for a PHREEQC ``record_key`` as a dict (or ``None``)."""
    if not record_key or manifest.empty or "phreeqc_record_key" not in manifest.columns:
        return None
    hit = manifest[manifest["phreeqc_record_key"].astype(str) == str(record_key)]
    return hit.iloc[0].to_dict() if not hit.empty else None

def _condition_representative_sample(data: pd.DataFrame, condition_key: str) -> dict:
    """First measured row belonging to ``condition_key`` (for metadata/status)."""
    ann = replicates.annotate(data)
    sub = ann[ann[replicates.CONDITION_KEY_COLUMN].astype(str) == str(condition_key)]
    return sub.iloc[0].to_dict() if not sub.empty else {}

# Project-specific (fly ash) explanation — only shown when the dataset actually
# uses OA/PF/GS condition codes, so the app stays generic for other experiments.
def _condition_code_descriptions_md(codes) -> str:
    """Markdown describing the given condition codes, sourced from the config dict.

    ``config.CONDITION_CODE_DESCRIPTIONS`` is the single source of truth — the UI
    never hard-codes the wording or the not-confirmed-sealed caution.
    """
    lines = []
    for code in sorted(codes):
        info = config.CONDITION_CODE_DESCRIPTIONS.get(str(code).strip().upper())
        if not info:
            continue
        caution = f" — ⚠️ {info['caution']}" if info.get("caution") else ""
        lines.append(f"- **{code} = {info['label']}**: {info['description']}{caution}")
    return "\n".join(lines)

def _dataset_condition_codes(data: pd.DataFrame) -> set[str]:
    """OA/PF/GS codes actually present in this run's measured rows (may be empty)."""
    codes: set[str] = set()
    if data is None or data.empty:
        return codes
    for _, r in data.iterrows():
        code = scenarios.sample_condition_code(r.to_dict())
        if code:
            codes.add(code)
    return codes

def _render_condition_code_help(data: pd.DataFrame) -> None:
    """Show the cup-cover condition descriptions *only* if this dataset uses the codes."""
    codes = _dataset_condition_codes(data)
    if not codes:
        return
    with st.expander(f"What do the condition codes ({', '.join(sorted(codes))}) mean? (this dataset)"):
        st.markdown(_condition_code_descriptions_md(codes))
        st.caption("CO₂-exposure cup covers. Source: config.CONDITION_CODE_DESCRIPTIONS.")

def _render_mapping_status_line(sample: dict, scenario: dict | None) -> str:
    """Show the four-state mapping status as a compact, color-coded line."""
    status = replicates.mapping_status(sample, scenario)
    st.markdown("**Mapping status:** " + app_ui.status_badge(status, status),
                unsafe_allow_html=True)
    st.caption(replicates.MAPPING_STATUS_DEFINITIONS.get(status, ""))
    return status

# Generic validation-metadata columns surfaced when present (project-agnostic).
_GENERIC_META_FIELDS = [
    "leachant", "acid_M", "NaOH_M", "time_min", "liquid_solid_ratio",
    "temperature_C", "CO2_condition",
]

def _render_advanced_validation_metadata(sample: dict, scenario: dict | None) -> None:
    """Expander body — measured-side metadata (dynamic) + model-side PHREEQC metadata.

    Metadata is never discarded (it stays on every saved row); this view only
    *surfaces* whatever columns the current dataset actually has, so the app does
    not assume every experiment carries OA/PF/GS, CO₂ cover, or leachant fields.
    """
    # --- Measured-side metadata, shown dynamically ---
    derived: dict = {}
    exp_code = scenarios.sample_condition_code(sample)
    if exp_code:  # project-specific (fly ash) derived fields, only when codes exist
        derived["condition_code"] = exp_code
        derived["cover_condition"] = scenarios.cover_condition(exp_code) or "—"
        derived["CO2_exposure_level"] = scenarios.co2_exposure_level(exp_code) or "—"
    for f in _GENERIC_META_FIELDS:
        if _is_present(sample.get(f)):
            derived[f] = sample.get(f)
    # any other non-canonical metadata the importer preserved (extra__* columns)
    for k, v in sample.items():
        if str(k).startswith("extra__") and _is_present(v) and k not in (
                "extra__condition_code", "extra__cover_condition", "extra__CO2_exposure_level"):
            derived[k] = v

    if exp_code:
        st.caption(
            "Condition codes, cover type and CO₂ exposure are **auto-derived** from the "
            "sample/condition name for this dataset — no manual selection needed."
        )
    else:
        st.caption("Validation metadata available for this dataset (varies by experiment).")
    if derived:
        st.dataframe(pd.DataFrame([derived]), use_container_width=True, height=80)
    else:
        st.info("No extra validation metadata columns found for this record.")
    # Cup-cover description + not-confirmed-sealed caution, from config (single source).
    if exp_code and str(exp_code).upper() in config.CONDITION_CODE_DESCRIPTIONS:
        st.markdown(_condition_code_descriptions_md({exp_code}))

    if not scenario:
        st.info("Pick a model / simulation result above to see matched / missing / conflicting fields.")
        return

    # --- Model-side metadata (currently PHREEQC) ---
    st.markdown("**Model prediction source (PHREEQC)**")
    pheq_source = {
        "file/source": scenario.get("source_file", "—"),
        "solution number": scenario.get("solution_number", "—"),
        "state": scenario.get("state", "—"),
        "scenario": scenario.get("scenario_label", "—"),
    }
    st.dataframe(pd.DataFrame([pheq_source]), use_container_width=True, height=80)

    pheq_fields = ["liquid_solid_ratio", "CO2_condition", "time_min",
                   "condition_code", "NaOH_M", "temperature_C"]
    avail = {f: ("present" if _is_present(scenario.get(f)) else "not specified")
             for f in pheq_fields}
    st.markdown("**Model metadata availability**")
    st.dataframe(pd.DataFrame([avail]), use_container_width=True, height=80)

    score = scenarios.score_scenario(sample, scenario)
    st.markdown(
        f"- **Matched fields:** {', '.join(score['matched_fields']) or '—'}\n"
        f"- **Missing model fields:** {', '.join(score.get('phreeqc_missing', [])) or '—'}\n"
        f"- **Conflicting fields:** {', '.join(score['mismatched_fields']) or '—'}"
    )
    for note in score.get("metadata_notes", []):
        st.caption("ℹ️ " + note)

# Status → a badged display string for the table's status column / detail headers.
def _status_badge(status: str) -> str:
    emoji = _MAPPING_STATUS_BADGE.get(status, ("•", "info"))[0]
    return f"{emoji} {status}"

def _map_flash(run_name: str, level: str, msg: str) -> None:
    """Queue a message that should survive the post-accept rerun (shown atop the table)."""
    st.session_state.setdefault(f"map_flash_{run_name}", []).append((level, msg))

def _render_map_flash(run_name: str) -> None:
    for level, msg in st.session_state.pop(f"map_flash_{run_name}", []):
        getattr(st, level, st.info)(msg)

# Metadata fields compared side-by-side in the row-detail alignment table.
_ALIGNMENT_FIELDS = ["liquid_solid_ratio", "CO2_condition", "temperature_C",
                     "time_min", "NaOH_M"]

def _fmt_meta(value) -> str:
    return str(value) if _is_present(value) else "—"

def _alignment_value_table(sample: dict, scenario: dict) -> pd.DataFrame:
    """Measured vs model values for each comparison field (both sides, with values)."""
    rows = [{"field": f, "measured": _fmt_meta(sample.get(f)),
             "model (PHREEQC)": _fmt_meta(scenario.get(f))} for f in _ALIGNMENT_FIELDS]
    rows.append({
        "field": "condition_code",
        "measured": scenarios.sample_condition_code(sample) or "—",
        "model (PHREEQC)": _fmt_meta(scenario.get("condition_code")),
    })
    return pd.DataFrame(rows)

def _accept_condition_mappings(run_name: str, rows: list[dict], *,
                               override: bool = False) -> int:
    """Save the chosen suggestion rows as condition mappings, then apply to per-sample.

    Each row needs a ``condition_key`` + ``phreeqc_record_key``. ``override=True`` tags
    the saved mapping (used for the confirmed unsafe manual-override path). Replicates
    inherit the link via :func:`run_manager.apply_condition_mapping`.
    """
    accepted = 0
    for row in rows:
        ck = str(row.get("condition_key", "")).strip()
        key = str(row.get("phreeqc_record_key", "")).strip()
        if not ck or not key:
            continue
        status = row.get("mapping_status", "")
        note = (f"override of {status} mapping" if override
                else f"accepted from suggestion table ({status})")
        try:
            run_manager.add_condition_mapping(run_name, ck, key, notes=note,
                                              override=override, mapping_status=status)
            accepted += 1
        except run_manager.RunManagerError as exc:
            st.error(f"{ck}: {exc}")
    if accepted:
        try:
            run_manager.apply_condition_mapping(run_name)
        except run_manager.RunManagerError as exc:
            st.error(str(exc))
    return accepted

# Columns shown in the editable suggestion table (status is badged for display).
_SUGGESTION_DISPLAY_COLUMNS = [
    "accept", "condition_key", "n_replicates", "scenario_label",
    "phreeqc_record_key", "status", "score", "confidence", "already_mapped", "reason",
]

def _runner_up_delta(best_score: int, candidate: dict) -> str:
    """One-line 'lost N points: …' for a runner-up, generated from its trace."""
    lost = int(best_score) - int(candidate.get("score") or 0)
    # Prefer the explicit conflicts; fall back to the model-missing metadata.
    why = list(candidate.get("mismatched_fields") or [])
    if not why:
        why = [f"model lacks {m}" for m in candidate.get("phreeqc_missing", [])]
    detail = "; ".join(why) if why else "lower-scoring metadata match"
    if lost <= 0:
        return f"tied with best · {detail}"
    return f"lost {lost} point(s) vs best: {detail}"

def _render_condition_detail(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame,
                             condition_key: str) -> None:
    """Row-level detail: structured alignment + runner-up candidates + score breakdown."""
    sample, candidates = mapping_table.condition_candidates(data, condition_key, manifest, top_n=3)
    # Cup-cover condition description (with the not-confirmed-sealed caution), from
    # config — shown only when this condition actually carries an OA/PF/GS code.
    _det_code = scenarios.sample_condition_code(sample)
    if _det_code and str(_det_code).upper() in config.CONDITION_CODE_DESCRIPTIONS:
        st.caption(_condition_code_descriptions_md({_det_code}))
    if not candidates:
        st.info("No model/simulation candidate exists for this condition — it needs a new simulation.")
        return
    best_score = candidates[0].get("score") or 0
    for i, c in enumerate(candidates):
        scenario = mapping_table._manifest_row(manifest, c["suggested_phreeqc_record_key"])
        status = replicates.mapping_status(sample, scenario)
        header = "Best candidate" if i == 0 else f"Runner-up {i}"
        with st.container(border=True):
            st.markdown(
                f"**{header}: {c.get('scenario_label', '') or '—'}**  ·  score "
                f"{c['score']}  ·  {c['confidence']} confidence  ·  {_status_badge(status)}"
            )
            st.caption(f"`{c['suggested_phreeqc_record_key']}`")
            # Runner-up one-line delta vs the best candidate (generated from the trace).
            if i > 0:
                st.caption("↳ " + _runner_up_delta(best_score, c))
            if c.get("confidence_explanation"):
                st.caption(f"Confidence: {c['confidence_explanation']}")

            st.caption("Field alignment (measured vs model, with values):")
            st.dataframe(_alignment_value_table(sample, scenario or {}),
                         use_container_width=True, hide_index=True, height=250)
            st.markdown(
                f"- **Matched:** {', '.join(c['matched_fields']) or '—'}\n"
                f"- **Missing model fields:** {', '.join(c.get('phreeqc_missing', [])) or '—'}\n"
                f"- **Conflicting (both values):** {', '.join(c['mismatched_fields']) or '—'}"
            )
            breakdown = c.get("score_breakdown", [])
            if breakdown:
                st.caption("Score breakdown (which rule added/subtracted points):")
                st.dataframe(pd.DataFrame(breakdown), use_container_width=True,
                             hide_index=True, height=min(40 + 35 * len(breakdown), 220))
            for note in c.get("metadata_notes", []):
                st.caption("ℹ️ " + note)

def _render_suggestion_table(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame,
                             table: pd.DataFrame) -> None:
    """Automatic-first suggestion table (no button) + review detail + accept actions."""
    _render_map_flash(run_name)
    st.markdown("#### Suggested mappings")
    st.caption(
        "One row per measured condition, generated automatically with transparent "
        "rule-based scoring (no ML). Replicates of a condition are mapped together. "
        "Tick **accept** then **Accept selected**; **Accept all exact** bulk-accepts only "
        "exact matches. Unsafe rows cannot be accepted here — use Manual override below."
    )
    if table.empty:
        st.info("No measured data groups to map yet.")
        return

    EXACT = replicates.MAPPING_STATUS_EXACT
    UNSAFE = replicates.MAPPING_STATUS_UNSAFE
    NEEDS_NEW = replicates.MAPPING_STATUS_NEEDS_NEW

    disp = table.copy()
    # Pre-tick exact, not-yet-mapped rows; never pre-tick unsafe/needs-new.
    disp.insert(0, "accept", [
        (r["mapping_status"] == EXACT and not bool(r["already_mapped"]))
        for _, r in disp.iterrows()
    ])
    disp["status"] = disp["mapping_status"].map(_status_badge)
    edited = st.data_editor(
        disp[_SUGGESTION_DISPLAY_COLUMNS],
        column_config={
            "accept": st.column_config.CheckboxColumn("accept", help="Tick to accept this mapping"),
            "reason": st.column_config.TextColumn("reason", width="large"),
        },
        disabled=[c for c in _SUGGESTION_DISPLAY_COLUMNS if c != "accept"],
        hide_index=True, use_container_width=True, height=320,
        key=f"sug_editor_{run_name}",
    )

    # Re-attach the raw status to each row (the editor only edits `accept`).
    records = table.to_dict("records")
    for rec, acc in zip(records, edited["accept"].tolist()):
        rec["accept"] = bool(acc)

    a1, a2, a3 = st.columns(3)
    if a1.button("✅ Accept all exact suggestions", key=f"acc_exact_{run_name}"):
        rows = mapping_table.exact_suggestions(table).to_dict("records")
        n = _accept_condition_mappings(run_name, rows)
        _map_flash(run_name, "success", f"Accepted {n} exact mapping(s).")
        _read_csv.clear()
        st.rerun()
    if a2.button("✅ Accept selected", key=f"acc_sel_{run_name}"):
        selected = [r for r in records if r["accept"]]
        acceptable = [r for r in selected
                      if r["mapping_status"] in mapping_table.SELECTABLE_STATUSES
                      and str(r["phreeqc_record_key"]).strip()]
        unsafe_sel = [r for r in selected if r["mapping_status"] == UNSAFE]
        new_sel = [r for r in selected if r["mapping_status"] == NEEDS_NEW]
        n = _accept_condition_mappings(run_name, acceptable)
        _map_flash(run_name, "success", f"Accepted {n} selected mapping(s).")
        if any(r["mapping_status"] == replicates.MAPPING_STATUS_SCENARIO for r in acceptable):
            _map_flash(run_name, "info",
                       "Some accepted mappings are **scenario-level only** — comparison graphs "
                       "for them are a preliminary / workflow check, not final validation.")
        if unsafe_sel:
            _map_flash(run_name, "error",
                       f"{len(unsafe_sel)} **unsafe** row(s) were NOT accepted. Unsafe mappings "
                       "(e.g. acid leachant on a NaOH/CO₂ scenario) can only be saved via "
                       "**Manual override / advanced mapping**, where a confirmation is required "
                       "and the mapping is tagged `override=true`.")
        if new_sel:
            _map_flash(run_name, "warning",
                       f"{len(new_sel)} row(s) are **needs new simulation** — no candidate to accept.")
        _read_csv.clear()
        st.rerun()
    if a3.button("➡️ Export mapping to pipeline", key=f"exp_sug_{run_name}"):
        try:
            dest = run_manager.export_mapping_to_pipeline(run_name)
            _map_flash(run_name, "success",
                       f"Copied mapping to {_rel(dest)} — step 05 reads it.")
            _read_csv.clear()
        except run_manager.RunManagerError as exc:
            _map_flash(run_name, "error", str(exc))
        st.rerun()

    st.markdown("**Inspect a condition** — alignment, runner-up candidates & score breakdown")
    sel = st.selectbox("Condition to inspect", table["condition_key"].astype(str).tolist(),
                       key=f"detail_sel_{run_name}")
    _render_condition_detail(run_name, data, manifest, sel)

def _render_saved_condition_mappings(run_name: str) -> None:
    """Review of accepted/saved condition mappings (+ advanced apply/delete)."""
    cond_map = run_manager.read_condition_mapping(run_name)
    st.markdown(f"**Accepted / saved mappings** ({len(cond_map)})")
    if cond_map.empty:
        st.caption("No mappings accepted yet — use auto-suggest above, or manual override below.")
        return
    st.dataframe(cond_map, use_container_width=True, height=150)
    _render_condition_mapping_advanced(run_name, cond_map)

def _render_condition_mapping(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame) -> None:
    """Manual override view: condition + scenario + status + notes + save/apply."""
    summary = replicates.replicate_summary(data)
    if summary.empty:
        st.info("No conditions to map yet — enter data first.")
        return

    condition_keys = summary["condition_key"].tolist()
    c1, c2 = st.columns([2, 3])
    with c1:
        sel_condition = st.selectbox("Measured data group", condition_keys,
                                     key=f"cond_sel_{run_name}",
                                     help="A group of measured records sharing one experimental "
                                          "condition (its replicates are mapped together).")
    with c2:
        chosen_key = _scenario_record_key_picker(run_name, manifest, key=f"cond_scn_{run_name}")

    sample = _condition_representative_sample(data, sel_condition)
    scenario = _manifest_row(manifest, chosen_key)
    status = _render_mapping_status_line(sample, scenario)

    notes = st.text_input(
        "Notes (optional)", key=f"cond_notes_{run_name}",
        help="Free-text validation context. Stored with the mapping; never sent to "
             "the comparison step.",
    )

    # Unsafe mappings can ONLY be saved here, and only with explicit confirmation;
    # the saved mapping is tagged override=true in the condition-mapping CSV.
    is_unsafe = (status == replicates.MAPPING_STATUS_UNSAFE)
    override_confirm = False
    if is_unsafe:
        st.error(
            "This mapping is **unsafe** — a known metadata conflict (e.g. an acid leachant "
            "mapped to a NaOH/CO₂ scenario, or opposite CO₂ families). It cannot be accepted "
            "from the suggestion table; saving it here records `override=true`."
        )
        override_confirm = st.checkbox(
            "I understand this mapping is unsafe and want to override and save it anyway.",
            key=f"cond_override_{run_name}",
        )

    if st.button("Save mapping", key=f"cond_map_{run_name}", type="primary"):
        if not chosen_key:
            st.warning("Pick a model / simulation result first.")
        elif is_unsafe and not override_confirm:
            st.warning("Tick the override confirmation to save an unsafe mapping.")
        else:
            try:
                run_manager.add_condition_mapping(
                    run_name, sel_condition, chosen_key, notes=notes, override=is_unsafe)
                run_manager.apply_condition_mapping(run_name)
                n = len(run_manager.read_mapping(run_name))
                st.success(
                    f"Saved & applied — condition `{sel_condition}` → `{chosen_key}` "
                    f"({n} sample row(s) mapped)."
                    + (" Tagged `override=true`." if is_unsafe else "")
                )
                if status != replicates.MAPPING_STATUS_EXACT:
                    st.info(
                        "This mapping is not *exact*, so any comparison graph is a "
                        "**preliminary / workflow check only**."
                    )
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))

    with st.expander("Advanced validation metadata"):
        _render_advanced_validation_metadata(sample, scenario)

def _render_condition_mapping_advanced(run_name: str, cond_map: pd.DataFrame) -> None:
    """Advanced apply-options (replicate→solution) + delete, kept out of the main view."""
    with st.expander("Advanced apply options & replicate→solution mapping"):
        st.warning(
            "⚠️ Only use replicate→solution mapping if sol1/sol2/sol3 represent **replicate "
            "batches**, not time points or unrelated solutions."
        )
        st.caption("Map each replicate id to a PHREEQC solution number (e.g. R1→1, R2→2), "
                   "then re-apply to point each replicate at its own solution.")
        rc1, rc2, rc3 = st.columns(3)
        rep_in = rc1.text_input("replicate_id (e.g. R1)", key=f"repsol_rid_{run_name}")
        sol_in = rc2.text_input("solution_number (e.g. 1)", key=f"repsol_sol_{run_name}")
        if rc3.button("Add R→sol", key=f"repsol_add_{run_name}"):
            try:
                run_manager.add_replicate_solution(run_name, rep_in, sol_in)
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))
        rs_map = run_manager.read_replicate_solution_map(run_name)
        if not rs_map.empty:
            st.dataframe(rs_map, use_container_width=True, height=120)
        use_rep_sol = st.checkbox(
            "Re-apply using replicate→solution mapping (instead of one scenario for all replicates)",
            key=f"repsol_use_{run_name}",
        )
        if st.button("Re-apply condition mapping → per-sample map", key=f"cond_apply_{run_name}"):
            try:
                path = run_manager.apply_condition_mapping(run_name, use_replicate_solution=use_rep_sol)
                n = len(run_manager.read_mapping(run_name))
                st.success(f"Applied — {n} sample row(s) written to `{_rel(path)}`.")
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))

    with st.expander("Delete condition mappings"):
        to_del = st.multiselect(
            "Rows to delete", options=list(range(len(cond_map))),
            format_func=lambda i: f"{cond_map.iloc[i]['condition_key']} → "
                                  f"{cond_map.iloc[i]['phreeqc_record_key']}",
            key=f"cond_del_{run_name}",
        )
        if st.button("Delete selected condition mappings", key=f"cond_delbtn_{run_name}") and to_del:
            run_manager.delete_condition_mapping_rows(run_name, to_del)
            _read_csv.clear()
            st.rerun()

def _render_replicate_collision_warnings(data: pd.DataFrame, mapping: pd.DataFrame,
                                         manifest: pd.DataFrame) -> None:
    """Feature 7 — replicate-aware mapping safety warnings."""
    warns = replicates.collision_report(data, mapping, manifest)
    if not warns:
        st.success("No replicate-aware mapping problems. (Replicates of one condition sharing a "
                   "PHREEQC scenario is expected, not a collision.)")
        return
    # Keep the (often repeated) detail tidy for presentation.
    st.warning(f"⚠️ {len(warns)} replicate-aware mapping warning(s) — expand for detail.")
    with st.expander("Mapping warning detail"):
        seen: set[str] = set()
        for w in warns:
            if w["message"] in seen:
                continue
            seen.add(w["message"])
            st.markdown(f"- {w['message']}")

def _render_mapping_section(run_name: str) -> None:
    """Measured-data → model-prediction mapping UI for a lab-like run.

    Automatic-first: a single suggestion table is generated as soon as run data +
    model results exist (no button). Workflow order: **suggestion table → accept →
    existing mappings → conditions needing simulation → advanced tools**. Manual
    dropdown mapping, the scenario explorer and the per-sample assistant are kept
    under expanders. Saves to the run's own ``data/sample_phreeqc_map.csv``.
    """
    st.markdown("---")
    st.subheader("Match measured data to model predictions")
    st.caption(
        "Automatic-first: a **suggestion table** is built as soon as data + model results "
        "exist — review, accept, then graph. Transparent rule-based scoring (no ML); "
        "manual mapping, scenario explorer and per-sample assistant are under advanced."
    )

    data = run_manager.read_data_file(run_name)

    sample_ids: list[str] = []
    if "sample_id" in data.columns:
        for s in data["sample_id"].astype(str).map(str.strip).tolist():
            if s and s.lower() != "nan":
                sample_ids.append(s)
    sample_ids = list(dict.fromkeys(sample_ids))  # unique, order-preserving

    # Item 1 — no measured data: a clear message, and no stale suggestions/state.
    if not sample_ids:
        for k in (f"map_flash_{run_name}", f"sug_editor_{run_name}", f"detail_sel_{run_name}"):
            st.session_state.pop(k, None)
        st.info(
            "No measured data found for this run. Upload or import a dataset in the "
            "**Data** tab first."
        )
        return

    graph_note = (
        "Graphs only require measured values, model-predicted values, and a saved "
        "mapping between them. Extra metadata is retained for scientific validation "
        "and interpretation."
    )
    if _dataset_condition_codes(data):
        graph_note += (
            "\n\nFor this dataset, OA/PF/GS and CO₂ exposure are treated as "
            "validation metadata."
        )
    st.info(graph_note)

    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if not results_path.exists():
        st.info(
            "`data/processed/phreeqc_results.csv` not found — run Phase 1 first "
            "(the Compare Results tab) to generate model (PHREEQC) results."
        )
        return
    phreeqc = _read_csv(str(results_path), results_path.stat().st_mtime)
    if "record_key" not in phreeqc.columns:
        st.warning("`phreeqc_results.csv` has no `record_key` column — cannot map.")
        return

    manifest = _scenario_manifest(str(results_path), results_path.stat().st_mtime)
    mapping = run_manager.read_mapping(run_name)
    cond_map = run_manager.read_condition_mapping(run_name)

    # Build the suggestion table ONCE; the table view and the "needs new simulation"
    # section are both driven from it, so their counts always agree.
    table = mapping_table.build_suggestion_table(data, manifest, cond_map)
    if not table.empty:
        _sc = table["mapping_status"].value_counts().to_dict()
        _audit_once(
            run_name, "suggestion:" + ":".join(f"{k}={v}" for k, v in sorted(_sc.items())),
            lambda: audit.log_suggestion_table(
                run_name, status_counts=_sc, n_conditions=int(len(table))))

    # Compact mapping summary — accepted + the status distribution across the
    # detected measured conditions (same source as the suggestion table below).
    if not table.empty:
        vc = table["mapping_status"].value_counts().to_dict()
        _ms = replicates
        app_ui.render_metric_cards([
            {"label": "Accepted mappings", "value": len(cond_map),
             "status": "good" if len(cond_map) else "neutral"},
            {"label": "Exact", "value": vc.get(_ms.MAPPING_STATUS_EXACT, 0),
             "status": "exact" if vc.get(_ms.MAPPING_STATUS_EXACT, 0) else "neutral"},
            {"label": "Scenario-level", "value": vc.get(_ms.MAPPING_STATUS_SCENARIO, 0),
             "status": "scenario-level" if vc.get(_ms.MAPPING_STATUS_SCENARIO, 0) else "neutral"},
            {"label": "Unsafe", "value": vc.get(_ms.MAPPING_STATUS_UNSAFE, 0),
             "status": "unsafe" if vc.get(_ms.MAPPING_STATUS_UNSAFE, 0) else "neutral"},
            {"label": "Needs new sim", "value": vc.get(_ms.MAPPING_STATUS_NEEDS_NEW, 0),
             "status": "needs new simulation" if vc.get(_ms.MAPPING_STATUS_NEEDS_NEW, 0) else "neutral"},
        ])

    # ---- 1) Suggestion table (auto-generated) + accept actions ----
    _render_suggestion_table(run_name, data, manifest, table)

    # ---- 2) Existing/accepted mappings + overall status ----
    st.markdown("---")
    _render_saved_condition_mappings(run_name)
    overall = replicates.overall_mapping_status(data, mapping, manifest)
    if not mapping.empty:
        line = " · ".join(f"{k}: {v}" for k, v in overall.get("counts", {}).items())
        if overall.get("all_exact"):
            st.success(f"Overall mapping status: **{overall.get('overall')}** — {line}")
        else:
            st.warning(
                f"Overall mapping status: **{overall.get('overall')}** — {line}. "
                "Comparison graphs are a **preliminary / workflow check only** until "
                "mappings are exact."
            )

    # ---- 3) Conditions needing new simulations (same table source) ----
    st.markdown("---")
    _render_sim_needed(run_name, data, mapping, manifest, table)

    _render_condition_code_help(data)

    # ---- 4) Advanced tools (all functionality kept, just demoted) ----
    with st.expander("Manual override / advanced mapping"):
        st.caption(
            "Pick a measured data group and model result by hand if a suggestion is wrong. "
            "**Unsafe** mappings can only be saved here, with a confirmation that records "
            "`override=true`."
        )
        _render_condition_mapping(run_name, data, manifest)
        st.markdown("---")
        st.markdown("**Per-sample manual mapping**")
        _render_manual_mapping(run_name, phreeqc, sample_ids)

    with st.expander("Validation context: replicate summary & mapping warnings"):
        _render_replicate_summary(data)
        st.markdown("---")
        _render_mapping_quality(mapping)
        _render_replicate_collision_warnings(data, mapping, manifest)

    with st.expander("Explore PHREEQC scenarios"):
        _render_scenario_explorer(run_name, manifest)

    with st.expander("Per-sample assistant (advanced)"):
        _render_mapping_assistant(run_name, data, sample_ids, manifest)

    with st.expander("Per-sample mappings: view, delete & export to pipeline"):
        st.markdown(f"**Existing per-sample mappings** ({len(mapping)}):")
        st.dataframe(mapping, use_container_width=True, height=170)
        if not mapping.empty:
            def _map_label(i: int) -> str:
                r = mapping.iloc[i]
                return f"Row {i} — {r.get('sample_id', '')} → {r.get('phreeqc_record_key', '')}"
            to_del = st.multiselect(
                "Select mapping rows to delete", options=list(range(len(mapping))),
                format_func=_map_label, key=f"map_del_{run_name}",
            )
            confirm = st.checkbox(
                "I understand this will delete the selected mapping rows.",
                key=f"map_delc_{run_name}",
            )
            if st.button("Delete selected mappings", key=f"map_delbtn_{run_name}"):
                if not to_del:
                    st.warning("No mapping rows selected — nothing was deleted.")
                elif not confirm:
                    st.warning("Tick the confirmation checkbox before deleting.")
                else:
                    n = run_manager.delete_mapping_rows(run_name, to_del)
                    st.success(f"Deleted {n} mapping row(s).")
                    st.rerun()

            if st.button("➡️ Export mapping to pipeline", key=f"map_export_{run_name}"):
                try:
                    dest = run_manager.export_mapping_to_pipeline(run_name)
                    st.success(
                        f"Copied mapping to {_rel(dest)} — step 05 "
                        "will use it to compute residuals."
                    )
                    _read_csv.clear()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))

def _render_match_tab(selected_run: str | None) -> None:
    app_ui.render_page_header(
        "Match — link measured data to model predictions",
        f"Automatic-first: records are detected and model-prediction mappings are suggested "
        f"for you (current model: {MODEL_NAME}). Review, then accept. Manual mapping stays "
        "available under advanced.",
        eyebrow="Validation module · Match",
    )
    _render_next_step(selected_run)
    app_ui.render_workflow_steps(
        ["Auto-detect measured records", "Auto-suggest model mappings",
         "Review suggestions", "Accept mappings"],
        current=1,
    )
    if not selected_run:
        st.info(
            "Select or create a **lab_experiment** (or **plastic_composite**) run in the "
            "sidebar to add a measured → model mapping."
        )
        return
    rt = run_manager.load_run_config(selected_run).get("run_type")
    if rt == "literature_benchmark":
        st.info(
            "Literature benchmark runs do not use sample-to-model mapping as measured "
            "lab data."
        )
        return
    if rt not in run_manager.LAB_LIKE_RUN_TYPES:
        st.info(
            "Mapping is only available for **lab_experiment** or **plastic_composite** "
            "runs. The current run is a synthetic/demo run (testing only)."
        )
        return
    _render_mapping_section(selected_run)


# Tab entry point (app.py calls ui.match_tab.render).
render = _render_match_tab
