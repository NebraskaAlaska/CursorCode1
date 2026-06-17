"""Simulate tab — describe -> parse -> plan -> material/release/database ->
input preview -> gated run + plots -> ranking/refinement -> target matching.

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

import io
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import app_ui  # noqa: E402  (presentation-only UI helper layer)
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import import_mapping  # noqa: E402
from flyash_phreeqc_ml import materials  # noqa: E402  (Simulate material-composition manager)
from flyash_phreeqc_ml.ai import config as ai_config  # noqa: E402  (AI settings/status authority)
from flyash_phreeqc_ml.ai import literature as ai_literature  # noqa: E402  (sourced lit values)
from flyash_phreeqc_ml.ai import scenario_parser as ai_scenario_parser  # noqa: E402  (NL simulation planner)
from flyash_phreeqc_ml.simulation import scenario_schema as sim_schema  # noqa: E402
from flyash_phreeqc_ml.simulation import matrix as sim_matrix  # noqa: E402
from flyash_phreeqc_ml.simulation import phreeqc_input_builder  # noqa: E402  (deterministic .pqi preview)
from flyash_phreeqc_ml.simulation import source_terms as sim_source_terms  # noqa: E402  (release model)
from flyash_phreeqc_ml.simulation import phase_templates as sim_phase_templates  # noqa: E402
from flyash_phreeqc_ml.simulation import database_compatibility as sim_dbcompat  # noqa: E402
from flyash_phreeqc_ml.simulation import phreeqc_executor  # noqa: E402  (gated deterministic execution)
from flyash_phreeqc_ml.simulation import batch_executor  # noqa: E402  (small-sweep execution)
from flyash_phreeqc_ml.simulation import run_registry  # noqa: E402  (simulation run provenance)
from flyash_phreeqc_ml.simulation import strategy as sim_strategy  # noqa: E402  (objective + ranking)
from flyash_phreeqc_ml.simulation import target_matching as sim_target  # noqa: E402  (inverse search)

from ui.state import _rel

# --------------------------------------------------------------------------- #
# Simulate tab — natural-language simulation planner (no PHREEQC execution)
# --------------------------------------------------------------------------- #
def _sim_num_text(label: str, value, key: str):
    """A text input for a numeric field that preserves 'missing' (blank → None)."""
    raw = st.text_input(label, value=("" if value is None else str(value)), key=key)
    return sim_schema.as_float(raw)

def _simulate_edit_form(flat: dict) -> dict:
    """Editable widgets for the key scenario fields; returns an edited flat dict.

    Numeric fields use text inputs so a blank stays 'missing' (None) rather than 0.
    """
    edited = dict(flat)
    c1, c2 = st.columns(2)
    with c1:
        edited["material_name"] = (st.text_input(
            "Material", value=flat.get("material_name") or "", key="sim_e_mat") or None)
        edited["solid_mass_g"] = _sim_num_text("Solid mass (g)", flat.get("solid_mass_g"), "sim_e_mass")
        edited["liquid_volume_mL"] = _sim_num_text(
            "Liquid volume (mL)", flat.get("liquid_volume_mL"), "sim_e_vol")
        edited["leachant_type"] = (st.text_input(
            "Leachant", value=flat.get("leachant_type") or "", key="sim_e_lea") or None)
        edited["leachant_concentration_M"] = _sim_num_text(
            "Leachant concentration (M)", flat.get("leachant_concentration_M"), "sim_e_conc")
    with c2:
        edited["time_min"] = _sim_num_text("Time (min)", flat.get("time_min"), "sim_e_time")
        edited["temperature_C"] = _sim_num_text(
            "Temperature (°C)", flat.get("temperature_C"), "sim_e_temp")
        co2_opts = ["(unknown)"] + list(sim_schema.CO2_CONDITION_ALLOWED)
        cur = flat.get("CO2_condition") or "(unknown)"
        cur = cur if cur in co2_opts else "(unknown)"
        co2 = st.selectbox("CO2 condition", co2_opts, index=co2_opts.index(cur), key="sim_e_co2")
        edited["CO2_condition"] = None if co2 == "(unknown)" else co2
        edited["target_elements"] = st.multiselect(
            "Target elements", list(sim_schema.RECOGNIZED_ELEMENTS),
            default=[e for e in (flat.get("target_elements") or [])
                     if e in sim_schema.RECOGNIZED_ELEMENTS], key="sim_e_els")
        edited["desired_outputs"] = st.multiselect(
            "Desired outputs", list(sim_schema.DESIRED_OUTPUTS_VOCAB),
            default=[o for o in (flat.get("desired_outputs") or [])
                     if o in sim_schema.DESIRED_OUTPUTS_VOCAB], key="sim_e_outs")
    # L/S is recomputed from the (possibly edited) mass + volume when the scenario rebuilds.
    edited["liquid_solid_ratio"] = None
    return edited

_PREVIEW_STATUS_LEVEL = {
    phreeqc_input_builder.STATUS_READY: "exact",
    phreeqc_input_builder.STATUS_TEMPLATE_WARNING: "scenario-level",
    phreeqc_input_builder.STATUS_NEEDS_COMPOSITION: "preliminary",
    phreeqc_input_builder.STATUS_DRAFT: "preliminary",
    phreeqc_input_builder.STATUS_MISSING_FIELD: "unsafe",
    phreeqc_input_builder.STATUS_UNSUPPORTED_LEACHANT: "unsafe",
}

# --------------------------------------------------------------------------- #
# Step 7 — Material profile (composition) manager (session-only; off result path)
# --------------------------------------------------------------------------- #
_MP_STATUS_LEVEL = {
    materials.STATUS_VERIFIED: "exact",
    materials.STATUS_USER_CONFIRMED: "exact",
    materials.STATUS_LITERATURE_UNVERIFIED: "scenario-level",
    materials.STATUS_DRAFT: "preliminary",
}

_MP_OXIDE_STARTERS = ["SiO2", "Al2O3", "CaO", "Fe2O3", "MgO", "Na2O", "K2O", "SO3",
                      "TiO2", "P2O5"]

_MP_ELEMENT_STARTERS = ["Si", "Al", "Ca", "Fe", "Mg", "Na", "K", "Ti"]

def _mp_next_id(store: dict) -> str:
    n = st.session_state.get("sim_mp_counter", 0) + 1
    while f"mp{n}" in store:
        n += 1
    st.session_state["sim_mp_counter"] = n
    return f"mp{n}"

def _mp_basis_selector(key: str) -> str:
    return st.selectbox(
        "Composition basis", list(materials.KNOWN_BASES),
        format_func=lambda b: materials.BASIS_LABELS.get(b, b), key=key,
        help="How the numbers are expressed. Oxide wt % is converted to element wt % with "
             "gravimetric factors; mg/kg and mol/kg are element-based.")

def _mp_assay_table(profile) -> None:
    assays = profile.element_assays()
    if not assays:
        return
    df = pd.DataFrame(
        [{"element": a.element, "element wt %": round(a.value, 4), "from": a.source_species}
         for a in assays.values()])
    st.dataframe(df, hide_index=True, use_container_width=True, height=min(260, 60 + 28 * len(df)))

def _render_material_profile_section(scenario):
    """Step 7 — provide / review / confirm a material composition for the input preview.

    Returns the selected ``materials.MaterialProfile`` (or ``None``). Everything here is
    **session-only** (nothing written to disk), **off the scientific result path**, and a
    composition is **never invented** — only a profile the user *confirms* can feed the
    preview; otherwise it stays ``needs_material_composition``.
    """
    st.markdown("#### Step 7 — Material profile (composition)")
    st.caption(
        "A meaningful model prediction needs the **dissolved material composition**. Provide it "
        "here (oxide wt %, element wt %, mg/kg, or mol/kg). Composition is **never invented** — "
        "only a profile **you confirm** can feed the input preview. Profiles live in this "
        "session only, are **not** saved to disk, and do **not** affect any validation or "
        "comparison result.")

    store = st.session_state.setdefault("sim_material_profiles", {})

    with st.expander("➕ Create or import a material profile", expanded=not store):
        _material_profile_create(scenario, store)

    if not store:
        st.info("No material profile yet. Without a **confirmed** profile the input preview "
                "stays `needs_material_composition` — honest, because composition is never "
                "invented.")
        return None

    ids = list(store)
    labels = {pid: f"{store[pid].material_name}  ·  {store[pid].verification_status}"
              for pid in ids}
    options = ["(none — keep needs_material_composition)"] + ids
    choice = st.selectbox(
        "Material profile to use for this simulation", options,
        format_func=lambda o: o if o.startswith("(none") else labels.get(o, o),
        key="sim_mp_select")
    if choice.startswith("(none"):
        return None
    profile = store[choice]
    _material_profile_review(profile, store)
    return profile

def _material_profile_review(profile, store: dict) -> None:
    """Summary card + validation + resolved elements + confirm/delete controls (in place)."""
    res = materials.validate_profile(profile)
    st.markdown(
        f"**{profile.material_name}**  ·  basis `{profile.basis_label()}`  ·  "
        + app_ui.status_badge(
            materials.STATUS_LABELS.get(profile.verification_status, profile.verification_status),
            _MP_STATUS_LEVEL.get(profile.verification_status, "neutral")),
        unsafe_allow_html=True)

    if res.usable_for_preview:
        st.success("✓ Sufficient for the input preview — confirmed and it validates.")
    elif profile.is_usable and not res.ok:
        st.error("Confirmed, but the composition has errors — fix them before use.")
    elif res.requires_confirmation:
        st.warning("Literature-sourced and **unverified** — review values + citations, then "
                   "confirm to use.")
    else:
        st.warning("Draft — review the composition, then confirm to use it for the preview.")

    _mp_assay_table(profile)
    for e in res.errors:
        st.error(e)
    for w in res.warnings:
        st.warning(w)
    if res.infos:
        with st.expander("Validation notes"):
            for i in res.infos:
                st.caption("• " + i)
    if profile.source.citation:
        st.caption(f"Source: {profile.source.display()}")

    _material_profile_confirm_controls(profile, res)
    if st.button("Delete this profile", key=f"sim_mp_del_{profile.profile_id}"):
        store.pop(profile.profile_id, None)
        st.session_state.pop("sim_previews", None)
        st.session_state["sim_mp_select"] = "(none — keep needs_material_composition)"
        st.rerun()

def _material_profile_confirm_controls(profile, res) -> None:
    """Draft / literature → user-confirmed, with the appropriate acknowledgement gate."""
    if profile.is_usable:
        if st.button("Revert to draft", key=f"sim_mp_draft_{profile.profile_id}"):
            profile.verification_status = materials.STATUS_DRAFT
            st.session_state.pop("sim_previews", None)
            st.rerun()
        return
    if not res.can_confirm:
        st.caption("Fix the errors above before this profile can be confirmed.")
        return
    if res.requires_confirmation:                 # literature_unverified → double acknowledgement
        ack = st.checkbox(
            "I have reviewed every value **and its citation**, and I take responsibility for "
            "using this literature-sourced composition.", key=f"sim_mp_ack_{profile.profile_id}")
        if st.button("Confirm literature composition", disabled=not ack,
                     key=f"sim_mp_conf_lit_{profile.profile_id}"):
            profile.verification_status = materials.STATUS_USER_CONFIRMED
            st.session_state.pop("sim_previews", None)
            st.rerun()
    else:                                         # draft (manual / paste / upload)
        ack = st.checkbox(
            "I have reviewed this composition and confirm it for simulation planning.",
            key=f"sim_mp_ack_{profile.profile_id}")
        if st.button("Mark as user-confirmed", disabled=not ack,
                     key=f"sim_mp_conf_{profile.profile_id}"):
            profile.verification_status = materials.STATUS_USER_CONFIRMED
            st.session_state.pop("sim_previews", None)
            st.rerun()

def _material_profile_create(scenario, store: dict) -> None:
    """Build + save a new profile (manual / paste / upload / literature). Saves as DRAFT
    (or literature_unverified) — confirmation is a separate, deliberate step."""
    mode = st.radio("How will you provide composition?",
                    ["Manual entry", "Paste table", "Upload file", "Literature (AI)"],
                    horizontal=True, key="sim_mp_mode")
    if mode == "Literature (AI)":
        _material_profile_literature(scenario, store)
        return

    default_name = (scenario.material.material_name if scenario is not None else "") or ""
    default_type = (scenario.material.material_type if scenario is not None else "") or ""
    entries: list = []
    source = materials.CompositionSource(source_type=materials.SOURCE_USER_ENTERED,
                                         retrieved_by="manual")

    if mode == "Manual entry":
        basis = _mp_basis_selector("sim_mp_basis_m")
        starters = (_MP_OXIDE_STARTERS if basis == materials.BASIS_OXIDE_WT
                    else _MP_ELEMENT_STARTERS)
        seed = pd.DataFrame({"species": starters,
                             "value": [None] * len(starters),
                             "uncertainty": [None] * len(starters)})
        edited = st.data_editor(seed, num_rows="dynamic", use_container_width=True,
                                height=320, key="sim_mp_editor")
        entries = materials.entries_from_records(
            edited.to_dict("records"), species_key="species", value_key="value",
            uncertainty_key="uncertainty")
    elif mode == "Paste table":
        basis = _mp_basis_selector("sim_mp_basis_p")
        txt = st.text_area(
            "Paste 'species value' rows (one per line)", key="sim_mp_paste", height=160,
            placeholder="SiO2 38\nAl2O3 18\nCaO 24\nFe2O3 6\nMgO 5\nNa2O 1.8\nK2O 0.6")
        entries = materials.parse_composition_text(txt)
        source = materials.CompositionSource(source_type=materials.SOURCE_USER_ENTERED,
                                             retrieved_by="manual", source_reference="pasted table")
    else:  # Upload file
        basis = _mp_basis_selector("sim_mp_basis_u")
        entries, source = _material_profile_upload(store)

    c1, c2 = st.columns(2)
    name = c1.text_input("Material name", value=default_name, key="sim_mp_name")
    mtype = c2.text_input("Material type", value=default_type, key="sim_mp_type")
    c3, c4 = st.columns(2)
    with c3:
        loi = _sim_num_text("LOI (wt %)", None, "sim_mp_loi")
    with c4:
        moisture = _sim_num_text("Moisture (wt %)", None, "sim_mp_moist")
    ref = st.text_input("Source reference (dataset / note)", value=source.source_reference or "",
                        key="sim_mp_ref")

    if entries:
        st.caption(f"Parsed **{len(entries)}** component(s).")
        preview = materials.MaterialProfile(
            profile_id="(preview)", material_name=name or "(unnamed)",
            material_type=mtype or None, composition_basis=basis, entries=entries,
            loi_pct=loi, moisture_pct=moisture, source=source)
        res = materials.validate_profile(preview)
        _mp_assay_table(preview)
        for e in res.errors:
            st.error(e)
        for w in res.warnings[:8]:
            st.warning(w)

    if st.button("Save profile to session", disabled=not (entries and name),
                 key="sim_mp_save"):
        pid = _mp_next_id(store)
        source.source_reference = ref or source.source_reference
        store[pid] = materials.MaterialProfile(
            profile_id=pid, material_name=name, material_type=mtype or None,
            composition_basis=basis, entries=entries, loi_pct=loi, moisture_pct=moisture,
            source=source, verification_status=materials.STATUS_DRAFT)
        st.session_state["sim_mp_select"] = pid
        st.session_state.pop("sim_previews", None)
        st.success(f"Saved draft profile '{name}'. Review + confirm it above to use it.")
        st.rerun()

def _material_profile_upload(store: dict):
    """File-upload path → (entries, CompositionSource). Returns ([], source) until ready."""
    source = materials.CompositionSource(source_type=materials.SOURCE_UPLOADED_FILE,
                                         retrieved_by="upload")
    up = st.file_uploader("Composition file (.csv / .xlsx)", type=["csv", "xlsx", "xls"],
                          key="sim_mp_file")
    if up is None:
        return [], source
    source.source_reference = up.name
    try:
        kind = import_mapping.file_kind(up.name)
        sheet = None
        if kind == "excel":
            sheets = import_mapping.list_excel_sheets(io.BytesIO(up.getvalue()))
            sheet = st.selectbox("Sheet", sheets, key="sim_mp_sheet")
        raw = import_mapping.read_tabular(io.BytesIO(up.getvalue()), kind=kind, sheet=sheet)
    except Exception as exc:                      # noqa: BLE001 — report, never crash the tab
        st.error(f"Could not read the file: {type(exc).__name__}: {exc}")
        return [], source
    cols = list(raw.columns)
    if len(cols) < 2:
        st.warning("The file needs at least a species column and a value column.")
        return [], source
    c1, c2, c3 = st.columns(3)
    sp_col = c1.selectbox("Species column", cols, key="sim_mp_sp")
    val_col = c2.selectbox("Value column", cols,
                           index=min(1, len(cols) - 1), key="sim_mp_val")
    unc_col = c3.selectbox("Uncertainty column (optional)", ["(none)"] + cols, key="sim_mp_unc")
    entries = materials.entries_from_records(
        raw.to_dict("records"), species_key=sp_col, value_key=val_col,
        uncertainty_key=(None if unc_col == "(none)" else unc_col))
    return entries, source

def _material_profile_literature(scenario, store: dict) -> None:
    """Literature (AI) path — proposes a **quarantined** (unverified) profile only."""
    if not ai_literature.is_enabled():
        st.caption("AI literature retrieval is disabled (no API key detected). Enter "
                   "composition manually, paste a table, or upload a file instead.")
        return
    st.caption(ai_literature.LITERATURE_DATA_NOTICE)
    if not st.checkbox(ai_literature.LITERATURE_CONSENT_LABEL, key="sim_mp_lit_consent"):
        return
    name = st.text_input("Material name",
                         value=(scenario.material.material_name if scenario else "") or "",
                         key="sim_mp_lit_name")
    default_els = ((scenario.outputs.target_elements if scenario else None)
                   or list(config.RESIDUAL_ELEMENTS))
    els = st.multiselect(
        "Elements to look up", list(sim_schema.RECOGNIZED_ELEMENTS),
        default=[e for e in default_els if e in sim_schema.RECOGNIZED_ELEMENTS],
        key="sim_mp_lit_els")
    st.caption("Each value is stored **literature-unverified** and cannot feed the preview "
               "until you review its citation and explicitly confirm it.")
    if st.button("Suggest composition from literature (AI)", disabled=not (name and els),
                 key="sim_mp_lit_btn"):
        cand_dicts: list[dict] = []
        with st.spinner("Searching literature…"):
            for el in els:
                try:
                    cands = ai_literature.propose_starting_assay(name, el)
                except Exception as exc:          # noqa: BLE001 — degrade per element
                    st.warning(f"{el}: lookup failed ({type(exc).__name__})")
                    continue
                if cands:
                    c = cands[0]
                    cand_dicts.append({
                        "element": c.element or el, "value": c.value, "unit": c.unit,
                        "citation": c.source_link, "title": c.citation.title,
                        "year": c.citation.year})
        if not cand_dicts:
            st.warning("No sourced literature composition found. Enter it manually instead.")
            return
        pid = _mp_next_id(store)
        store[pid] = materials.profile_from_literature_candidates(
            pid, name, (scenario.material.material_type if scenario else None), cand_dicts)
        st.session_state["sim_mp_select"] = pid
        st.session_state.pop("sim_previews", None)
        st.success(f"Saved {len(cand_dicts)} literature-proposed value(s) as an **unverified** "
                   "profile. Review every citation and confirm it above before use.")
        st.rerun()

def _render_release_model_section(scenario, material_profile):
    """Step 7b — choose a conservative, reviewable material **release model** (source term).

    Returns a ``source_terms.DissolutionModel``. Default is **no release** — composition stays
    comment-only and no material elements enter the simulation. Nothing here runs PHREEQC.
    """
    st.markdown("#### Step 7b — Material release model (dissolution source term)")
    st.caption(
        "A confirmed material profile gives the **bulk** assay; PHREEQC needs to know how much "
        "**dissolves**. Choose a conservative, reviewable release model. **Default: no release** — "
        "the assay stays comment-only. Release fractions are **assumptions, not measured truth**, "
        "and the whole material is never silently dissolved.")

    if material_profile is None or not getattr(material_profile, "is_usable", False):
        st.info("Confirm a material profile in **Step 7** to enable a release model. Without one, "
                "**no material elements enter the simulation** (predicted Ca/Si/Al/Fe ≈ 0).")
        return sim_source_terms.no_release()

    elements = sorted(material_profile.element_assays().keys())
    mode = st.radio(
        "Release model", [
            "No material release (default)",
            "Global release fraction",
            "Per-element release fractions",
            "Measured liquid composition (input, not prediction)",
        ], key="sim_release_mode",
        help="No release is the safe default. A release fraction is the assumed % of each element "
             "that dissolves into the liquid. Measured-liquid uses your measured concentrations "
             "as input (not a prediction).")

    model = sim_source_terms.no_release()
    if mode.startswith("Global"):
        pct = st.number_input("Release fraction — % of each element that dissolves", 0.0, 100.0,
                              1.0, 0.5, key="sim_release_global",
                              help="e.g. 1% is a conservative starting assumption; 100% (full "
                                   "dissolution) is usually unrealistic.")
        allow = st.checkbox("Allow > 100% (nonphysical — releases more than is present)",
                            key="sim_release_over")
        model = sim_source_terms.global_release(pct / 100.0, allow_over_unity=allow)
    elif mode.startswith("Per-element"):
        default_pct = st.number_input("Default release fraction (%) for elements not overridden",
                                      0.0, 100.0, 1.0, 0.5, key="sim_release_default")
        seed = pd.DataFrame({"element": elements, "release_%": [default_pct] * len(elements)})
        edited = st.data_editor(seed, hide_index=True, use_container_width=True,
                                key="sim_release_table", height=min(320, 60 + 30 * len(elements)))
        per = {}
        for row in edited.to_dict("records"):
            v = sim_schema.as_float(row.get("release_%"))
            if v is not None:
                per[str(row.get("element"))] = v / 100.0
        model = sim_source_terms.global_release(default_pct / 100.0, per_element=per)
    elif mode.startswith("Measured"):
        st.caption("These are **measured input**, not a prediction. Enter measured dissolved "
                   "concentrations (mM); leave blank to omit.")
        seed = pd.DataFrame({"element": elements, "measured_mM": [None] * len(elements)})
        edited = st.data_editor(seed, hide_index=True, use_container_width=True,
                                key="sim_release_measured", height=min(320, 60 + 30 * len(elements)))
        meas = {}
        for row in edited.to_dict("records"):
            v = sim_schema.as_float(row.get("measured_mM"))
            if v is not None:
                meas[str(row.get("element"))] = v
        model = sim_source_terms.measured_liquid(meas)

    # Live preview of the computed source terms (no PHREEQC run)
    result = sim_source_terms.compute_source_terms(
        model, material_profile=material_profile, solid_mass_g=scenario.material.solid_mass_g,
        liquid_volume_mL=scenario.leachant.liquid_volume_mL)
    if result.status == sim_source_terms.STATUS_RELEASE_INCLUDED:
        st.success("✓ The input preview **will include** material source terms (a REACTION block).")
        df = pd.DataFrame([
            {"element": r.element, "release %": round(r.fraction * 100, 4),
             "released mol": float(f"{r.moles_released:.4g}"),
             "conc (mM)": (round(r.concentration_mM, 4) if r.concentration_mM is not None else None)}
            for r in result.released])
        st.dataframe(df, hide_index=True, use_container_width=True,
                     height=min(300, 60 + 30 * len(df)))
    elif result.status == sim_source_terms.STATUS_MEASURED_LIQUID:
        st.success("✓ The input preview will create the solution from your **measured** "
                   "concentrations (labelled measured input, not prediction).")
    else:
        st.caption("No material source terms will be added.")
    for w in result.warnings:
        st.warning(w.message)
    if result.assumptions:
        with st.expander("Source-term assumptions"):
            for a in result.assumptions:
                st.caption("• " + a)
    return model

_DB_LEVEL_STATUS = {
    sim_dbcompat.LEVEL_SUITABLE: "exact",
    sim_dbcompat.LEVEL_PARTIAL: "scenario-level",
    sim_dbcompat.LEVEL_BASIC_AQUEOUS: "preliminary",
    sim_dbcompat.LEVEL_UNKNOWN: "neutral",
}

def _render_database_phases_section(scenario):
    """Step 7c — show database compatibility + pick a reviewed candidate-phase template.

    Returns the selected ``phase_templates.PhaseTemplate``. Only phases the configured database
    actually defines will be added to the input (the builder enforces this); this section makes
    that transparent. Nothing here runs PHREEQC.
    """
    st.markdown("#### Step 7c — Database & candidate phases")
    st.caption(
        "Precipitation and saturation-index predictions depend on the **thermodynamic database** "
        "and the **candidate phases**. Only phases your configured database actually defines are "
        "added — **nothing is invented, and missing phases are never silently added**. Default: "
        "**aqueous only** (no precipitation modelled).")

    templates = list(sim_phase_templates.TEMPLATES)
    keys = [t.key for t in templates]
    labels = {t.key: t.label for t in templates}
    chosen = st.selectbox("Candidate phase template", keys,
                          format_func=lambda k: labels[k], key="sim_phase_template")
    template = sim_phase_templates.get_template(chosen)
    report = sim_dbcompat.build_report(expected_phases=template.phase_names())

    c1, c2, c3 = st.columns(3)
    c1.metric("Database", report.database_label)
    c2.metric("Detected family", report.detected_family)
    c3.metric("Present", "yes" if report.database_exists else "no")
    st.caption("Configured database path: "
               + (f"`{report.database_path}`" if report.database_path
                  else "`(none — set the PHREEQC_DATABASE environment variable)`"))
    st.markdown("Compatibility: " + app_ui.status_badge(
        report.compatibility_level.replace("_", " "),
        _DB_LEVEL_STATUS.get(report.compatibility_level, "neutral")), unsafe_allow_html=True)

    if template.is_aqueous_only:
        st.info("Aqueous-only — **no equilibrium phases**; only aqueous speciation + saturation "
                "indices are reported (no precipitation modelled).")
    else:
        if report.available_phases:
            st.success("Phases that **will be added** (defined in the database): "
                       + ", ".join(report.available_phases))
        if report.missing_phases:
            st.warning("Phases **skipped** (absent from the database — NOT added): "
                       + ", ".join(report.missing_phases))
        st.caption("Precipitation / SI interpretation is "
                   + ("**meaningful**." if report.precipitation_meaningful
                      else "**limited** (no available phases / no database)."))
    for w in report.warnings:
        st.warning(w)
    if template.note:
        st.caption("ℹ️ " + template.note)
    return template

def _release_model_key(model):
    """A hashable identity for a DissolutionModel, to invalidate cached previews on change."""
    if model is None:
        return None
    return (getattr(model, "mode", None), getattr(model, "global_fraction", None),
            tuple(sorted((getattr(model, "per_element", {}) or {}).items())),
            tuple(sorted((getattr(model, "measured_liquid_mM", {}) or {}).items())),
            getattr(model, "allow_over_unity", False), getattr(model, "confirmed", False))

def _render_phreeqc_input_preview(scenario, matrix, material_profile=None,
                                  dissolution_model=None, phase_template=None) -> None:
    """Deterministic PHREEQC input preview from a confirmed plan — in-memory, download-only.

    No PHREEQC is run, no file is written to a run folder, and AI does not write the input
    (the LLM only extracted the scenario; this `.pqi` text is templated by deterministic code
    in `simulation/phreeqc_input_builder.py`). ``material_profile`` is the user-confirmed
    composition and ``dissolution_model`` the user-chosen release model (or ``None`` → the
    composition stays comment-only and no material elements enter the system).
    """
    st.markdown("#### Step 8 — PHREEQC input preview (draft)")
    st.caption(
        f"**{phreeqc_input_builder.PREVIEW_HEADER_LABEL}** Deterministic, rule-based `.pqi` "
        "text — AI does not write PHREEQC input. Nothing is run and no file is written to a "
        "run folder; the preview is in-memory and downloadable only.")
    if scenario is None:
        st.info("Re-generate the plan (Step 6) to enable the input preview.")
        return

    # Which material profile (if any) feeds the composition, and is it usable?
    mpid = getattr(material_profile, "profile_id", None) if material_profile is not None else None
    if material_profile is None:
        st.caption("No material profile selected (Step 7) — composition is **not** included, so "
                   "the preview will stay `needs_material_composition`.")
    elif material_profile.is_usable:
        st.caption(f"Using confirmed material profile **{material_profile.display_name}** "
                   f"(basis `{material_profile.basis_label()}`) for the dissolved composition.")
    else:
        st.caption(f"Material profile **{material_profile.display_name}** is selected but **not "
                   "confirmed** — confirm it in Step 7 to include its composition.")
    # Stale-preview guard: rebuild if the profile, release model, or phase template changed.
    cache_key = (mpid, _release_model_key(dissolution_model),
                 getattr(phase_template, "key", None))
    if st.session_state.get("sim_previews_key", cache_key) != cache_key:
        st.session_state.pop("sim_previews", None)

    if st.button("Generate PHREEQC input preview", key="sim_pqi_btn"):
        with st.spinner("Templating PHREEQC input…"):
            st.session_state["sim_previews"] = phreeqc_input_builder.build_previews_for_matrix(
                scenario, matrix, material_profile=material_profile,
                dissolution_model=dissolution_model, phase_template=phase_template)
        st.session_state["sim_previews_key"] = cache_key
        st.session_state["sim_previews_mpid"] = mpid
    previews = st.session_state.get("sim_previews")
    if not previews:
        st.info("Click **Generate PHREEQC input preview** to template a draft `.pqi` per scenario.")
        return

    ids = [p.scenario_id for p in previews]
    chosen_id = st.selectbox("Scenario", ids, key="sim_pqi_choice") if len(ids) > 1 else ids[0]
    pv = next(p for p in previews if p.scenario_id == chosen_id)

    st.markdown(
        f"**{pv.scenario_id}** · template `{pv.template_type}` · "
        + app_ui.status_badge(pv.status.replace("_", " "),
                              _PREVIEW_STATUS_LEVEL.get(pv.status, "neutral")),
        unsafe_allow_html=True)
    st.warning("⚠️ " + phreeqc_input_builder.PREVIEW_HEADER_LABEL)
    st.code(pv.phreeqc_input_text, language="text")
    st.download_button(
        "Download .pqi", pv.phreeqc_input_text,
        file_name=f"{pv.scenario_id}_preview.pqi", mime="text/plain",
        key=f"sim_pqi_dl_{pv.scenario_id}")
    if pv.warnings:
        st.markdown("**Warnings**")
        for w in pv.warnings:
            st.warning(w)
    if pv.assumptions:
        st.markdown("**Assumptions**")
        for a in pv.assumptions:
            st.markdown(f"- {a}")
    if pv.unsupported_features:
        st.markdown("**Unsupported / not yet modeled**")
        for u in pv.unsupported_features:
            st.markdown(f"- {u}")

# --------------------------------------------------------------------------- #
# Step 9 — Run deterministic model (gated PHREEQC execution; off the result path)
# --------------------------------------------------------------------------- #
_EXEC_STATUS_LEVEL = {
    phreeqc_executor.STATUS_SUCCESS: "exact",
    phreeqc_executor.STATUS_FAILED: "unsafe",
    phreeqc_executor.STATUS_TIMEOUT: "unsafe",
    phreeqc_executor.STATUS_MISSING: "preliminary",
    phreeqc_executor.STATUS_NOT_RUN: "neutral",
}

def _render_run_deterministic_model(previews, matrix=None) -> None:
    """Step 9 — run PHREEQC on a reviewed input preview (explicit, gated; never automatic).

    Runs the **exact** reviewed input text. Results are simulation outputs, **not** validated
    predictions, and never touch mapping / residuals / validation / the comparison.
    """
    st.markdown("#### Step 9 — Run deterministic model")
    av = phreeqc_executor.check_availability()
    cfg_badge = app_ui.status_badge("PHREEQC configured" if av.can_run else "PHREEQC not configured",
                                    "exact" if av.can_run else "preliminary")
    st.markdown(cfg_badge, unsafe_allow_html=True)

    if not previews:
        st.info("Generate an input preview in **Step 8** first — then you can run it here.")
        return
    if not av.can_run:
        app_ui.render_warning_panel("PHREEQC execution is not configured", av.message,
                                    level="warning")
        st.caption("To enable execution, set `PHREEQC_EXE` (the `phreeqc` binary, or put it on "
                   "PATH) and `PHREEQC_DATABASE` (your CEMDATA18 `.dat` file — not shipped). The "
                   "planner, preview, and download all work fully without this.")
        return

    ids = [p.scenario_id for p in previews]
    default = st.session_state.get("sim_pqi_choice")
    idx = ids.index(default) if default in ids else 0
    chosen = st.selectbox("Scenario to run", ids, index=idx, key="sim_exec_choice") \
        if len(ids) > 1 else ids[0]
    pv = next(p for p in previews if p.scenario_id == chosen)

    st.markdown(
        f"Selected **{pv.scenario_id}** · input status "
        + app_ui.status_badge(pv.status.replace("_", " "),
                              _PREVIEW_STATUS_LEVEL.get(pv.status, "neutral")),
        unsafe_allow_html=True)
    if pv.status != phreeqc_input_builder.STATUS_READY:
        st.warning(f"This input is `{pv.status}` (not `ready_for_review`). You can still run it, "
                   "but the result will reflect the input's limitations (e.g. missing material "
                   "composition).")
    app_ui.render_warning_panel(
        "This runs deterministic PHREEQC",
        "It executes the reviewed input text exactly. It is not AI-generated output and is not "
        "validated against measured data yet. Output is a simulation result, not a validated "
        "prediction.", level="warning")
    st.caption(f"Files are written to a safe workspace: "
               f"`{_rel(phreeqc_executor.default_workspace())}` (gitignored — never "
               "`data/raw` or the source tree).")

    confirm = st.checkbox("I have reviewed this input and want to run PHREEQC on it.",
                          key=f"sim_exec_confirm_{chosen}")
    if st.button("Run PHREEQC", disabled=not confirm, key=f"sim_exec_btn_{chosen}"):
        with st.spinner("Running PHREEQC…"):
            result = phreeqc_executor.execute_preview(pv)
            parsed = (phreeqc_executor.parse_outputs(result)
                      if result.status == phreeqc_executor.STATUS_SUCCESS else None)
        st.session_state.setdefault("sim_exec_results", {})[chosen] = {
            "result": result, "parsed": parsed}

    stored = st.session_state.get("sim_exec_results", {}).get(chosen)
    if stored:
        _render_simulation_result(stored["result"], stored["parsed"])

    # --- small-sweep (batch) execution ----------------------------------- #
    if len(previews) > 1:
        st.divider()
        _render_run_sweep(previews, matrix)

    # --- save the run with full provenance (only once results exist) ----- #
    if _collect_simulate_batch() is not None:
        st.divider()
        _render_save_simulation_run(matrix)

def _render_run_sweep(previews, matrix) -> None:
    """Run a confirmed *small sweep* — every reviewed preview, capped at the prototype limit."""
    st.markdown("##### Run confirmed sweep")
    n = len(previews)
    maxn = batch_executor.DEFAULT_MAX_SCENARIOS
    n_run = min(n, maxn)
    st.caption(f"The plan has **{n}** scenario(s); each one invokes PHREEQC and may take a few "
               "seconds. This prototype runs small, confirmed sweeps only.")
    if n > maxn:
        app_ui.render_warning_panel(
            f"Large sweep — only the first {maxn} will run",
            f"{n} scenarios exceed the prototype limit of {maxn}. {batch_executor.LARGE_SWEEP_MESSAGE}",
            level="warning")
    app_ui.render_warning_panel(
        "This runs deterministic PHREEQC for each scenario",
        "It executes the reviewed inputs exactly — not AI-generated output, and not validated "
        "against measured data. Outputs are simulation results, not validated predictions.",
        level="warning")

    confirm = st.checkbox(
        f"I have reviewed these inputs and want to run {n_run} PHREEQC scenario(s).",
        key="sim_sweep_confirm")
    if st.button("Run confirmed sweep", disabled=not confirm, key="sim_sweep_btn"):
        prog = st.progress(0.0, text="Starting…")

        def _cb(i, total, sid, status):
            prog.progress(i / max(1, total), text=f"[{i}/{total}] {sid} — {status}")

        with st.spinner("Running sweep…"):
            batch = batch_executor.run_batch(previews, on_progress=_cb)
        prog.empty()
        st.session_state["sim_batch_result"] = batch
        st.session_state["sim_batch_matrix"] = matrix

    batch = st.session_state.get("sim_batch_result")
    if batch is not None:
        _render_sweep_results(batch, st.session_state.get("sim_batch_matrix"))

def _render_sweep_results(batch, matrix) -> None:
    """Status summary + batch table + dynamic plots (only because results exist)."""
    counts = batch.status_counts()
    st.markdown("**Execution status** — "
                + " · ".join(f"`{k}`: {v}" for k, v in counts.items()))
    if batch.truncated:
        st.warning(f"Ran the first {batch.executed} of {batch.requested} scenarios. "
                   + batch_executor.LARGE_SWEEP_MESSAGE)
    st.caption("📌 " + batch_executor.SWEEP_OUTPUT_LABEL)

    table = batch_executor.build_result_table(batch, matrix)
    st.dataframe(table, hide_index=True, use_container_width=True,
                 height=min(360, 60 + 30 * len(table)))
    st.download_button("Download sweep results (CSV)", table.to_csv(index=False),
                       file_name="simulation_sweep_results.csv", mime="text/csv",
                       key="sim_sweep_dl")
    _simulate_provenance_caption()
    _render_sweep_plots(batch, table, matrix)
    if batch.n_success > 0:
        st.divider()
        _render_rank_simulation_results(batch, table, matrix)

def _render_sweep_plots(batch, table, matrix) -> None:
    """pH-vs-sweep + element-totals-vs-sweep + status summary — drawn only when runs succeeded."""
    if batch.n_success == 0:
        st.info("No successful scenarios to plot. See the status column above for why.")
        return
    axis_col, axis_label = batch_executor.detect_sweep_axis(matrix)

    # 1) pH vs sweep parameter
    ph_frame = batch_executor.sweep_plot_frame(table, axis_col, "pH")
    if not ph_frame.empty:
        st.markdown(f"**Predicted pH vs {axis_label}** (simulation output)")
        st.pyplot(_sweep_line_figure(ph_frame, axis_label, "predicted pH", axis_col is None))

    # 2) element totals vs sweep parameter
    el_cols = [c for c in table.columns if c.endswith("_mM")]
    el_frames = {c[:-3]: batch_executor.sweep_plot_frame(table, axis_col, c) for c in el_cols}
    el_frames = {el: fr for el, fr in el_frames.items() if not fr.empty}
    if el_frames:
        st.markdown(f"**Predicted dissolved totals (mM) vs {axis_label}** (simulation output)")
        st.pyplot(_sweep_multi_line_figure(el_frames, axis_label, "mM", axis_col is None))

    # 3) status summary
    st.markdown("**Execution status summary**")
    st.bar_chart(pd.Series(batch.status_counts(), name="scenarios"))

    st.caption(batch_executor.SWEEP_OUTPUT_LABEL + " These are **separate** from the measured-vs-"
               "model pH / residual graphs in **Validate** / **Compare Results**, which a "
               "simulation run never changes.")

def _sweep_line_figure(frame, xlabel, ylabel, categorical_x):
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn
    fig, ax = plt.subplots(figsize=(7, 3.4))
    if categorical_x:
        ax.plot(range(len(frame)), frame["y"], "o-", color="#3b7dd8")
        ax.set_xticks(range(len(frame)))
        ax.set_xticklabels(frame["scenario_id"], rotation=45, ha="right", fontsize=8)
    else:
        ax.plot(frame["x"], frame["y"], "o-", color="#3b7dd8")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig

def _sweep_multi_line_figure(frames: dict, xlabel, ylabel, categorical_x):
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn
    fig, ax = plt.subplots(figsize=(7, 3.8))
    cmap = plt.get_cmap("tab10")
    for i, (label, fr) in enumerate(sorted(frames.items())):
        color = cmap(i % 10)
        if categorical_x:
            ax.plot(range(len(fr)), fr["y"], "o-", label=label, color=color)
        else:
            ax.plot(fr["x"], fr["y"], "o-", label=label, color=color)
    if categorical_x and frames:
        any_fr = next(iter(frames.values()))
        ax.set_xticks(range(len(any_fr)))
        ax.set_xticklabels(any_fr["scenario_id"], rotation=45, ha="right", fontsize=8)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout()
    return fig

# --------------------------------------------------------------------------- #
# Rank simulation results (optimise over executed predictions — not validation)
# --------------------------------------------------------------------------- #
_OBJECTIVE_KIND_LABELS = {
    sim_strategy.OBJ_MAXIMIZE: "Maximize a target element",
    sim_strategy.OBJ_MINIMIZE: "Minimize an impurity element",
    sim_strategy.OBJ_TARGET_PH: "Target a pH range",
    sim_strategy.OBJ_AVOID_PH: "Avoid an unsafe pH range",
    sim_strategy.OBJ_MINIMIZE_REAGENT: "Minimize reagent concentration",
    sim_strategy.OBJ_SELECTIVITY: "Maximize a selectivity ratio (A/B)",
    sim_strategy.OBJ_WEIGHTED: "Maximize one & minimize another (weighted)",
}

def _objective_editor(detected, table) -> "sim_strategy.SimulationObjective":
    """Editable objective widgets, pre-filled from the detected objective. Returns it."""
    elements = [c[:-3] for c in table.columns if c.endswith("_mM")] or ["Ca"]

    def _detected_el(direction):
        for m in detected.metrics:
            if m.direction == direction and m.column.endswith("_mM"):
                return m.column[:-3]
        return elements[0]

    def _detected_ph():
        for m in detected.metrics:
            if m.column == sim_strategy.PH_COLUMN and m.target_low is not None:
                return float(m.target_low), float(m.target_high)
        return 10.0, 12.0

    kinds = list(_OBJECTIVE_KIND_LABELS)
    default_kind = detected.kind if detected.kind in _OBJECTIVE_KIND_LABELS else sim_strategy.OBJ_MAXIMIZE
    kind = st.selectbox("Objective type", kinds, index=kinds.index(default_kind),
                        format_func=lambda k: _OBJECTIVE_KIND_LABELS[k], key="sim_rank_kind")

    if kind == sim_strategy.OBJ_MAXIMIZE:
        el = st.selectbox("Element to maximize", elements,
                          index=elements.index(_detected_el(sim_strategy.DIR_MAX))
                          if _detected_el(sim_strategy.DIR_MAX) in elements else 0,
                          key="sim_rank_maxel")
        return sim_strategy.maximize(el)
    if kind == sim_strategy.OBJ_MINIMIZE:
        el = st.selectbox("Element to minimize", elements, key="sim_rank_minel")
        return sim_strategy.minimize(el)
    if kind in (sim_strategy.OBJ_TARGET_PH, sim_strategy.OBJ_AVOID_PH):
        lo0, hi0 = _detected_ph()
        c1, c2 = st.columns(2)
        lo = c1.number_input("pH low", value=lo0, step=0.5, key="sim_rank_phlo")
        hi = c2.number_input("pH high", value=hi0, step=0.5, key="sim_rank_phhi")
        return (sim_strategy.target_ph(lo, hi) if kind == sim_strategy.OBJ_TARGET_PH
                else sim_strategy.avoid_ph(lo, hi))
    if kind == sim_strategy.OBJ_MINIMIZE_REAGENT:
        return sim_strategy.minimize_reagent()
    if kind == sim_strategy.OBJ_SELECTIVITY:
        c1, c2 = st.columns(2)
        a = c1.selectbox("Numerator (maximize)", elements, key="sim_rank_sela")
        b = c2.selectbox("Denominator (minimize)", elements,
                         index=min(1, len(elements) - 1), key="sim_rank_selb")
        return sim_strategy.selectivity(a, b)
    # weighted
    c1, c2 = st.columns(2)
    a = c1.selectbox("Maximize", elements, key="sim_rank_wmax")
    b = c2.selectbox("Minimize", elements, index=min(1, len(elements) - 1), key="sim_rank_wmin")
    return sim_strategy.weighted([
        sim_strategy.ObjectiveMetric(f"{a}_mM", sim_strategy.DIR_MAX, label=a),
        sim_strategy.ObjectiveMetric(f"{b}_mM", sim_strategy.DIR_MIN, label=b)])

def _render_rank_simulation_results(batch, table, matrix) -> None:
    """Rank executed results against a confirmed objective + suggest a refined sweep."""
    st.markdown("##### Rank simulation results")
    st.caption("🏷️ " + sim_strategy.RANKING_NOT_VALIDATION)

    detected = sim_strategy.parse_objective(st.session_state.get("sim_outputs", ""))
    st.markdown(f"**Objective detected from your desired outputs:** `{detected.kind}` — "
                f"{detected.display()}")
    for note in detected.notes:
        st.caption("• " + note)

    objective = _objective_editor(detected, table)
    st.caption(f"Will rank by: **{objective.display()}** — ranking never runs simulations.")
    confirm = st.checkbox("Confirm this objective for ranking.", key="sim_rank_confirm")
    if st.button("Rank results", disabled=not confirm, key="sim_rank_btn"):
        axis_col, _ = batch_executor.detect_sweep_axis(matrix)
        st.session_state["sim_ranking"] = sim_strategy.rank_results(table, objective,
                                                                    axis_col=axis_col)
        st.session_state["sim_ranking_axis"] = axis_col

    ranking = st.session_state.get("sim_ranking")
    if ranking is not None:
        _render_ranking(ranking, table, st.session_state.get("sim_ranking_axis"))

def _render_ranking(ranking, table, axis_col) -> None:
    if ranking.status == "no_successful_rows":
        st.info("No successfully-executed scenarios to rank.")
        return
    if ranking.status == "no_rankable_metrics":
        for w in ranking.warnings:
            st.warning(w)
        st.info("No rankable metric — pick an objective whose metric exists in the results.")
        _render_refined_sweep(ranking, table, axis_col)
        return

    st.success(f"Top scenario: **{ranking.top_scenario_id}** "
               f"(driven by **{ranking.driving_metric}**).")
    st.dataframe(ranking.ranked, hide_index=True, use_container_width=True,
                 height=min(320, 60 + 30 * len(ranking.ranked)))
    for w in ranking.warnings:
        st.warning(w)
    if ranking.tradeoffs:
        st.markdown("**Tradeoffs**")
        for t in ranking.tradeoffs:
            st.caption("⚖️ " + t)
    st.caption("🏷️ " + sim_strategy.RANKING_NOT_VALIDATION)
    _render_refined_sweep(ranking, table, axis_col)

def _render_refined_sweep(ranking, table, axis_col) -> None:
    sug = sim_strategy.suggest_refined_sweep(ranking, table, axis_col)
    if sug.kind == "none":
        return
    st.markdown("##### Suggested refined sweep")
    st.info(sug.message)
    if sug.rationale:
        st.caption(sug.rationale)

    plan = sim_strategy.refined_sweep_plan(
        sug, ranking, table, max_scenarios=batch_executor.DEFAULT_MAX_SCENARIOS)
    if plan.blocked or not plan.axis:
        st.warning(plan.info)
        st.caption(sim_strategy.LARGE_SCALE_MESSAGE)
        return

    st.markdown(f"**Suggested sweep parameter:** `{plan.axis}`")
    st.caption(plan.info)
    for w in plan.warnings:
        st.warning(w)
    _render_refined_matrix_builder(plan, ranking)

def _render_refined_matrix_builder(plan, ranking) -> None:
    """Editable refined values → confirmed, plan-only matrix (never auto-runs)."""
    default = ", ".join(f"{v:g}" for v in plan.values)
    raw = st.text_input(f"Refined `{plan.axis}` values (comma-separated, editable)",
                        value=default, key="sim_refine_vals")
    edited = [v for v in (sim_schema.as_float(x) for x in raw.split(",")) if v is not None]
    physical = [v for v in edited if sim_strategy.is_physical_value(plan.axis, v)]
    dropped = len(edited) - len(physical)
    if dropped:
        st.warning(f"Ignored {dropped} nonphysical value(s) for `{plan.axis}` (must be above its "
                   "physical floor).")
    cap = batch_executor.DEFAULT_MAX_SCENARIOS
    if len(physical) > cap:
        st.warning(f"{len(physical)} values exceed the small-sweep cap of {cap} — only the first "
                   f"{cap} will be used. {sim_strategy.LARGE_SCALE_MESSAGE}")
        physical = sorted(physical)[:cap]
    user_edited = sorted(round(v, 6) for v in physical) != list(plan.values)

    st.markdown(f"**New refined matrix:** {len(physical)} scenario(s) over `{plan.axis}` "
                "(plan-only until you review the input previews and run it).")
    st.caption("📌 " + sim_strategy.REFINEMENT_LABEL)

    replace = st.checkbox("Replace the current sweep plan with this refined matrix",
                          value=True, key="sim_refine_replace")
    confirm = st.checkbox("I have reviewed these refined values.", key="sim_refine_confirm")
    if st.button("Generate refined matrix", disabled=not (confirm and physical),
                 key="sim_refine_btn"):
        sc = st.session_state.get("sim_scenario")
        new_matrix = sim_matrix.build_simulation_matrix(sc, ranges={plan.axis: physical})
        st.session_state["sim_refined_matrix"] = new_matrix
        import datetime as _dt
        top_score = None
        try:
            if ranking.ranked is not None and not ranking.ranked.empty:
                top_score = float(ranking.ranked.iloc[0].get("score"))
        except Exception:                                    # noqa: BLE001
            top_score = None
        st.session_state["sim_refinement"] = {
            "parent_run_id": st.session_state.get("sim_saved_run_id"),
            "parent_top_scenario_id": ranking.top_scenario_id,
            "objective": ranking.objective.display() if ranking.objective else None,
            "objective_kind": ranking.objective.kind if ranking.objective else None,
            "ranking_top_score": top_score,
            "reason": plan.info,
            "suggestion_kind": plan.kind,
            "axis": plan.axis,
            "suggested_values": list(plan.values),
            "applied_values": list(physical),
            "user_edited": bool(user_edited),
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        if replace:
            st.session_state["sim_matrix"] = new_matrix
            for k in ("sim_previews", "sim_previews_mpid", "sim_batch_result",
                      "sim_batch_matrix", "sim_exec_results", "sim_ranking",
                      "sim_ranking_axis", "sim_saved_run_id"):
                st.session_state.pop(k, None)
            st.success("Replaced the plan with the refined matrix. Review the input previews "
                       "(Step 8) and run it (Step 9) — **nothing runs automatically**.")
            st.rerun()
        else:
            st.success("Built a refined matrix below (it did **not** replace your current plan).")

    refined = st.session_state.get("sim_refined_matrix")
    if refined is not None and not replace:
        st.markdown("**Refined matrix (plan-only)**")
        st.dataframe(refined, hide_index=True, use_container_width=True,
                     height=min(240, 60 + 30 * len(refined)))
        st.download_button("Download refined plan (CSV)", refined.to_csv(index=False),
                           file_name="refined_simulation_plan.csv", mime="text/csv",
                           key="sim_refine_dl")
    st.caption(sim_strategy.LARGE_SCALE_MESSAGE)

# --------------------------------------------------------------------------- #
# Save-run provenance (Simulate executions → outputs/simulation_runs/)
# --------------------------------------------------------------------------- #
def _selected_material_profile():
    """The material profile currently selected in Step 7 (or None)."""
    store = st.session_state.get("sim_material_profiles", {})
    pid = st.session_state.get("sim_mp_select")
    if not pid or str(pid).startswith("(none"):
        return None
    return store.get(pid)

def _collect_simulate_batch():
    """A BatchResult of whatever has been executed (sweep preferred, else single runs)."""
    batch = st.session_state.get("sim_batch_result")
    if batch is not None and getattr(batch, "results", None):
        return batch
    singles = st.session_state.get("sim_exec_results", {})
    results = [batch_executor.BatchScenarioResult(sid, v["result"], v.get("parsed"))
               for sid, v in singles.items() if v.get("result")]
    if results:
        return batch_executor.BatchResult(results=results, requested=len(results),
                                          max_scenarios=batch_executor.DEFAULT_MAX_SCENARIOS)
    return None

def _simulate_provenance_caption() -> None:
    """A one-line provenance trail under a Simulate result table/plot."""
    bits = ["source: PHREEQC execution of reviewed input"]
    rid = st.session_state.get("sim_saved_run_id")
    if rid:
        bits.append(f"run id `{rid}`")
    mp = _selected_material_profile()
    bits.append(f"material profile: {mp.display_name} ({mp.verification_status})"
                if mp is not None else "material profile: none")
    pr = st.session_state.get("sim_parse_result")
    if pr is not None:
        bits.append(f"parser: {getattr(pr, 'source', 'manual')}")
    av = phreeqc_executor.check_availability()
    if av.executable_path:
        bits.append(f"exe `{av.executable_path}`")
    if av.database_path:
        bits.append(f"db `{av.database_path}`")
    st.caption("🔎 Provenance — " + " · ".join(bits)
               + ".  **Not validated against measured data.**")

def _render_save_simulation_run(matrix) -> None:
    """Step 9 — save the executed run (single or sweep) with full provenance."""
    batch = _collect_simulate_batch()
    if batch is None:
        return
    st.markdown("##### Save simulation run")
    reg = run_registry.SimulationRunRegistry()
    counts = batch.status_counts()
    st.caption("Save this run with its full provenance chain — experiment text → parsed "
               "scenario → assumptions → material profile → generated input → executable / "
               "database → output files → parser status → warnings.")
    st.markdown("**What will be saved:** "
                + f"{len(batch.results)} scenario(s) — "
                + " · ".join(f"`{k}`: {v}" for k, v in counts.items())
                + " · `run_metadata.json` · `parsed_results.csv` · `scenario_matrix.csv` · "
                "`assumptions_warnings.json`")
    st.caption(f"**Where:** `{_rel(reg.base_dir)}/<run_id>/` — a gitignored generated-output "
               "folder (never `data/raw`, the source tree, or any validation CSV).")
    if _selected_material_profile() is None:
        st.warning("No material profile is selected — that is recorded as a **warning** in the "
                   "saved run (composition was not included).")
    app_ui.render_warning_panel(
        "This saves a simulation result, not a validation",
        "It records a PHREEQC simulation run for provenance. It does not validate the model "
        "against measured data and never affects mapping / residuals / validation / comparison.",
        level="warning")

    label = st.text_input("Run label (optional)", key="sim_save_label")
    notes = st.text_area("Notes (optional)", key="sim_save_notes", height=68)
    confirm = st.checkbox("I understand this saves a simulation run (not a validation result).",
                          key="sim_save_confirm")
    if st.button("Save simulation run", disabled=not confirm, key="sim_save_btn"):
        import datetime as _dt
        now = _dt.datetime.now()
        rid = run_registry.generate_run_id(now, label)
        record = run_registry.build_run_record(
            run_id=rid, created_at=now.isoformat(timespec="seconds"), batch=batch, matrix=matrix,
            scenario=st.session_state.get("sim_scenario"),
            parse_result=st.session_state.get("sim_parse_result"),
            material_profile=_selected_material_profile(),
            previews=st.session_state.get("sim_previews"),
            experiment_text=st.session_state.get("sim_desc"),
            desired_outputs_text=st.session_state.get("sim_outputs"), label=label, notes=notes,
            refinement=st.session_state.get("sim_refinement"))
        try:
            out_dir = reg.save_run(record)
            st.session_state["sim_saved_run_id"] = rid
            st.success(f"Saved simulation run `{rid}` → `{_rel(out_dir)}`.")
        except Exception as exc:                              # noqa: BLE001
            st.error(f"Could not save the run: {type(exc).__name__}: {exc}")

    rid = st.session_state.get("sim_saved_run_id")
    if rid and reg.run_dir(rid).is_dir():
        _render_saved_run_downloads(reg, rid)

def _render_saved_run_downloads(reg, rid) -> None:
    d = reg.run_dir(rid)
    meta = d / run_registry.RUN_METADATA_FILE
    results = d / run_registry.PARSED_RESULTS_FILE
    c1, c2, c3 = st.columns(3)
    if meta.is_file():
        c1.download_button("run_metadata.json", meta.read_text(encoding="utf-8"),
                           file_name="run_metadata.json", mime="application/json",
                           key=f"sim_dl_meta_{rid}")
    if results.is_file():
        c2.download_button("parsed_results.csv", results.read_text(encoding="utf-8"),
                           file_name="parsed_results.csv", mime="text/csv",
                           key=f"sim_dl_res_{rid}")
    try:
        c3.download_button("Download run package (zip)", reg.export_zip(rid),
                           file_name=f"{rid}.zip", mime="application/zip",
                           key=f"sim_dl_zip_{rid}")
    except Exception:                                         # noqa: BLE001
        pass

# --------------------------------------------------------------------------- #
# Step 10 — Target matching (inverse simulation search; gated, capped, off result path)
# --------------------------------------------------------------------------- #
_TARGET_KINDS = [sim_target.TARGET_RANGE, sim_target.TARGET_VALUE, sim_target.TARGET_MAXIMIZE,
                 sim_target.TARGET_MINIMIZE, sim_target.TARGET_CONSTRAINT]

_TARGET_COLUMN_OPTIONS = (["pH"] + [f"{e}_mM" for e in sim_target.KNOWN_ELEMENTS]
                          + ["leachant_concentration_M", "release_fraction"])

def _target_spec_editor(detected) -> "sim_target.TargetSpec":
    """Editable target table (pre-filled from the detected spec); rebuild a TargetSpec from it."""
    with st.expander("Edit / set the target", expanded=not detected.is_defined):
        rows = [{"use": True, "kind": m.kind, "column": m.column, "label": m.label,
                 "op": m.op or "<", "low": m.low, "high": m.high, "value": m.value,
                 "tolerance": m.tolerance, "threshold": m.threshold, "weight": m.weight}
                for m in detected.metrics]
        if not rows:
            rows = [{"use": True, "kind": sim_target.TARGET_RANGE, "column": "pH", "label": "pH",
                     "op": "<", "low": 10.0, "high": 12.0, "value": None, "tolerance": None,
                     "threshold": None, "weight": 1.0}]
        edited = st.data_editor(
            pd.DataFrame(rows), num_rows="dynamic", use_container_width=True, hide_index=True,
            key="sim_tm_editor", column_config={
                "use": st.column_config.CheckboxColumn("use"),
                "kind": st.column_config.SelectboxColumn("kind", options=_TARGET_KINDS),
                "column": st.column_config.SelectboxColumn("column", options=_TARGET_COLUMN_OPTIONS),
                "label": st.column_config.TextColumn("label"),
                "op": st.column_config.SelectboxColumn("op", options=["<", "<=", ">", ">="]),
                "low": st.column_config.NumberColumn("low"),
                "high": st.column_config.NumberColumn("high"),
                "value": st.column_config.NumberColumn("value"),
                "tolerance": st.column_config.NumberColumn("± tol"),
                "threshold": st.column_config.NumberColumn("threshold"),
                "weight": st.column_config.NumberColumn("weight", min_value=0.0),
            })
        st.caption("`column` is `pH` or `<Element>_mM` (e.g. `Ca_mM`). **range** needs low+high · "
                   "**target_value** needs value (+ optional ± tol) · **constraint** needs op + "
                   "threshold · **maximize**/**minimize** need only a column.")
    metrics = []
    for _, r in edited.iterrows():
        if not bool(r.get("use", True)):
            continue
        kind = str(r.get("kind") or "").strip()
        col = str(r.get("column") or "").strip()
        if kind not in _TARGET_KINDS or not col:
            continue
        metrics.append(sim_target.TargetMetric(
            kind=kind, column=col, label=(str(r.get("label") or "").strip() or col),
            low=sim_schema.as_float(r.get("low")), high=sim_schema.as_float(r.get("high")),
            value=sim_schema.as_float(r.get("value")),
            tolerance=sim_schema.as_float(r.get("tolerance")),
            op=((str(r.get("op")).strip() or None) if kind == sim_target.TARGET_CONSTRAINT
                else None),
            threshold=sim_schema.as_float(r.get("threshold")),
            weight=sim_schema.as_float(r.get("weight")) or 1.0))
    return sim_target.target_from_metrics(metrics, source=(detected.source or "manual"))

def _build_target_previews(candidates, *, material_profile, phase_template, base_release_model):
    """One deterministic `.pqi` preview per candidate (its own release fraction → source term).

    A candidate that varies the release fraction gets ``global_release(fraction)``; one that
    only varies concentration inherits the Step-7b release model. Pure templating — nothing runs.
    """
    previews = []
    for c in candidates:
        sc = sim_schema.SimulationScenario.from_flat_dict(c.scenario_flat)
        dm = (sim_source_terms.global_release(c.release_fraction)
              if c.release_fraction is not None else base_release_model)
        previews.append(phreeqc_input_builder.build_phreeqc_input_preview(
            sc, scenario_id=c.scenario_id, material_profile=material_profile,
            dissolution_model=dm, phase_template=phase_template))
    return previews

def _render_score_breakdown(bd) -> None:
    st.markdown(f"**{bd.scenario_id}** — objective score {bd.objective_score:g} · "
                + ("**feasible**" if bd.feasible else "**infeasible**"))
    if bd.metric_scores:
        st.markdown("Objective metrics")
        for ms in bd.metric_scores:
            v = ms.get("value")
            vtxt = f"{v:g}" if isinstance(v, (int, float)) else "—"
            st.caption(f"• {ms['label']} ({ms['kind']}): value {vtxt} → score "
                       f"{ms['score']:g} (weight {ms['weight']:g})")
    if bd.constraint_results:
        st.markdown("Constraints")
        for cr in bd.constraint_results:
            v = cr.get("value")
            vtxt = f"{v:g}" if isinstance(v, (int, float)) else "—"
            sat = cr.get("satisfied")
            icon = "✅" if sat is True else ("❌" if sat is False else "❓")
            st.caption(f"{icon} {cr['display']} — value {vtxt}")

def _render_target_match_results(result) -> None:
    """Best candidate + ranked table + per-candidate breakdown + honesty captions."""
    st.markdown("##### Results")
    if result.status == sim_target.MATCH_NO_ROWS:
        st.info("No successfully-executed candidates to rank — see the inputs / configuration.")
        for w in result.warnings:
            st.warning(w)
        return
    if result.status == sim_target.MATCH_NO_METRICS:
        for w in result.warnings:
            st.warning(w)
        st.info("No target metric could be scored against the executed results (nothing fabricated).")
        return

    best = result.best or {}
    feasible = best.get("feasible")
    head = (f"Best match: **{best.get('scenario_id')}** · objective score "
            f"**{best.get('objective_score')}** · "
            + ("**feasible** (all constraints met)" if feasible
               else "**does not meet all constraints**"))
    (st.success if feasible else st.warning)(head)
    bits = []
    if best.get("leachant_concentration_M") is not None:
        bits.append(f"conc {best['leachant_concentration_M']:g} M")
    if best.get("release_fraction") is not None:
        bits.append(f"release {best['release_fraction'] * 100:g}%")
    for col, val in (best.get("values") or {}).items():
        if isinstance(val, (int, float)):
            bits.append(f"{col} {val:g}")
    if bits:
        st.caption("Best candidate — " + " · ".join(bits))
    st.caption(f"{result.n_feasible} of {len(result.ranked)} executed candidate(s) meet every "
               "constraint.")

    st.dataframe(result.ranked, hide_index=True, use_container_width=True,
                 height=min(360, 60 + 30 * len(result.ranked)))
    st.download_button("Download match results (CSV)", result.ranked.to_csv(index=False),
                       file_name="target_match_results.csv", mime="text/csv", key="sim_tm_dl")
    for w in result.warnings:
        st.warning(w)
    if result.breakdowns:
        ids = [b.scenario_id for b in result.breakdowns]
        chosen = st.selectbox("Score breakdown for candidate", ids, key="sim_tm_bd")
        bd = next((b for b in result.breakdowns if b.scenario_id == chosen), None)
        if bd is not None:
            _render_score_breakdown(bd)
    st.caption("🏷️ " + sim_target.NOT_VALIDATION + "  " + sim_target.DEPENDS_ON_RANGES)

def _render_save_target_run(state, material_profile) -> None:
    """Save the inverse search with full provenance — separate from validation runs."""
    batch, meta, result = state["batch"], state["meta"], state["result"]
    st.markdown("##### Save target-search run")
    reg = run_registry.SimulationRunRegistry()
    counts = batch.status_counts()
    st.caption("Saves the search with full provenance — target spec, search parameters, candidate "
               "grid, scoring method, best candidate, and warnings. Kept **separate** from "
               "measured-data validation runs.")
    st.markdown("**What will be saved:** "
                + f"{len(batch.results)} candidate(s) — "
                + " · ".join(f"`{k}`: {v}" for k, v in counts.items())
                + " · `run_metadata.json` (incl. `target_match`) · `parsed_results.csv` · "
                "`scenario_matrix.csv` · `assumptions_warnings.json`")
    app_ui.render_warning_panel(
        "This saves a simulation search, not a validation",
        "It records an inverse target search over model predictions under your assumptions. It "
        "does not validate the model against measured data and never affects mapping / residuals "
        "/ validation / comparison.", level="warning")

    label = st.text_input("Run label (optional)", key="sim_tm_save_label")
    notes = st.text_area("Notes (optional)", key="sim_tm_save_notes", height=68)
    confirm = st.checkbox("I understand this saves a simulation search (not a validation result).",
                          key="sim_tm_save_confirm")
    if st.button("Save target-search run", disabled=not confirm, key="sim_tm_save_btn"):
        import datetime as _dt
        now = _dt.datetime.now()
        rid = run_registry.generate_run_id(now, label or "target-search")
        prov = sim_target.target_match_provenance(
            state["spec"], state["params"], state["candidates"], result,
            created_at=now.isoformat(timespec="seconds"), max_scenarios=state["cap"],
            truncated=state["truncated"])
        record = run_registry.build_run_record(
            run_id=rid, created_at=now.isoformat(timespec="seconds"), batch=batch, matrix=meta,
            scenario=st.session_state.get("sim_scenario"),
            parse_result=st.session_state.get("sim_parse_result"),
            material_profile=material_profile, previews=None,
            experiment_text=st.session_state.get("sim_desc"),
            desired_outputs_text=st.session_state.get("sim_outputs"), label=label, notes=notes,
            target_match=prov)
        try:
            out_dir = reg.save_run(record)
            st.session_state["sim_tm_saved_run_id"] = rid
            st.success(f"Saved target-search run `{rid}` → `{_rel(out_dir)}`.")
        except Exception as exc:                              # noqa: BLE001
            st.error(f"Could not save the run: {type(exc).__name__}: {exc}")

    rid = st.session_state.get("sim_tm_saved_run_id")
    if rid and reg.run_dir(rid).is_dir():
        _render_saved_run_downloads(reg, rid)

def _render_target_matching(scenario, material_profile, phase_template, base_release_model) -> None:
    """Step 10 — inverse search: define a target, build a small reviewed grid, run + rank.

    Off the scientific result path: it imports no comparison/mapping module, runs PHREEQC only on
    an explicit click, caps the grid, never invents a target value or a release fraction, and never
    calls a result 'validated'. A match ranking is inverse search over model predictions.
    """
    st.markdown("#### Step 10 — Target matching (inverse search)")
    st.caption(
        "Work backwards from a desired result: define a **target** (a pH range, a target element "
        "value, an element to maximise/minimise, or a constraint like 'Fe below 0.1 mM'), choose "
        "a few **reviewed parameter values** to try, then — on an explicit click — run the small "
        "grid and rank each candidate by how well it matches.")
    st.caption("🏷️ " + sim_target.NOT_VALIDATION)
    if scenario is None:
        st.info("Generate a plan (Step 6) first.")
        return

    # 1) target spec ------------------------------------------------------- #
    st.markdown("##### 1 · Target")
    detected = sim_target.parse_target_spec(st.session_state.get("sim_outputs", ""))
    st.markdown(f"**Detected from your desired outputs:** {detected.display()}")
    for n in detected.notes:
        st.caption("• " + n)
    spec = _target_spec_editor(detected)
    st.caption((f"Will match against: **{spec.display()}**") if spec.is_defined
               else "No target set — add at least one row in the editor above.")

    # 2) search parameters ------------------------------------------------- #
    st.markdown("##### 2 · Search parameters (reviewed values to try)")
    st.caption("📌 " + sim_target.RELEASE_FRACTION_ASSUMPTION)
    cur = sim_schema.as_float(getattr(scenario.leachant, "leachant_concentration_M", None))
    conc_default = (", ".join(f"{v:g}" for v in sorted({round(cur * f, 4) for f in (0.5, 1.0, 2.0)}))
                    if cur else "0.1, 0.5, 1.0")
    conc_raw = st.text_input(
        "Leachant concentration values (M, comma-separated — blank = keep fixed)",
        value=conc_default, key="sim_tm_conc")
    rf_raw = st.text_input(
        "Global release fraction values (%, comma-separated — blank = keep current release model)",
        value="0.5, 1, 2", key="sim_tm_rf",
        help="e.g. 0.1, 0.5, 1, 2, 5 — compares how much of the material you assume dissolves.")

    params = []
    conc_vals = [v for v in (sim_schema.as_float(x) for x in conc_raw.split(",")) if v and v > 0]
    if conc_vals:
        params.append(sim_target.scenario_parameter(
            "leachant_concentration_M", conc_vals, label="leachant_concentration_M"))
    rf_vals = [v / 100.0 for v in (sim_schema.as_float(x) for x in rf_raw.split(",")) if v and v > 0]
    if rf_vals:
        params.append(sim_target.release_fraction_parameter(rf_vals))

    cap = batch_executor.DEFAULT_MAX_SCENARIOS
    candidates, truncated = sim_target.build_search_grid(scenario, params, max_scenarios=cap)
    if not candidates:
        st.info("Add at least one parameter value (concentration or release fraction) to build a "
                "search grid.")
        return
    st.markdown(f"**Search grid:** {len(candidates)} candidate scenario(s) — plan-only, capped at "
                f"{cap}.")
    if truncated:
        st.warning(f"The full grid exceeds the cap of {cap} — only the first {cap} candidates are "
                   "kept. " + sim_target.LARGE_SEARCH_MESSAGE)
    st.dataframe(sim_target.grid_preview_frame(candidates), hide_index=True,
                 use_container_width=True, height=min(280, 60 + 30 * len(candidates)))
    st.caption(sim_target.LARGE_SEARCH_MESSAGE)
    if material_profile is None or not getattr(material_profile, "is_usable", False):
        st.warning("No confirmed material profile (Step 7) — material composition is not included, "
                   "so predicted element totals will be ~0 and element targets cannot be matched. "
                   "Confirm a profile to make element matching meaningful (pH-only targets still "
                   "work).")

    # 3) run --------------------------------------------------------------- #
    st.markdown("##### 3 · Run the search")
    if not spec.is_defined:
        st.info("Define a target (step 1) before running the search.")
        return
    av = phreeqc_executor.check_availability()
    if not av.can_run:
        app_ui.render_warning_panel(
            "PHREEQC execution is not configured",
            av.message + " Target matching runs PHREEQC on each candidate; the grid above is "
            "plan-only. Set `PHREEQC_EXE` + `PHREEQC_DATABASE` to enable it.", level="warning")
        return
    app_ui.render_warning_panel(
        "This runs deterministic PHREEQC for each candidate",
        f"It executes {len(candidates)} reviewed input(s) exactly — not AI-generated, not "
        "validated against measured data. Nothing runs automatically.", level="warning")
    confirm = st.checkbox(
        f"I have reviewed the target + grid and want to run {len(candidates)} PHREEQC scenario(s).",
        key="sim_tm_confirm")
    if st.button("Run target search", disabled=not confirm, key="sim_tm_run"):
        prog = st.progress(0.0, text="Starting…")

        def _cb(i, total, sid, status):
            prog.progress(i / max(1, total), text=f"[{i}/{total}] {sid} — {status}")

        with st.spinner("Building inputs + running candidates…"):
            previews = _build_target_previews(
                candidates, material_profile=material_profile, phase_template=phase_template,
                base_release_model=base_release_model)
            batch = batch_executor.run_batch(previews, max_scenarios=cap, on_progress=_cb)
        prog.empty()
        meta = sim_target.candidate_metadata_frame(candidates)
        table = batch_executor.build_result_table(batch, meta)
        if sim_target.RELEASE_FRACTION_COLUMN not in table.columns:
            table = table.merge(meta[["scenario_id", sim_target.RELEASE_FRACTION_COLUMN]],
                                on="scenario_id", how="left")
        result = sim_target.score_results(spec, table)
        st.session_state["sim_tm_state"] = {
            "batch": batch, "meta": meta, "table": table, "result": result,
            "candidates": candidates, "params": params, "spec": spec, "cap": cap,
            "truncated": truncated}
        st.session_state.pop("sim_tm_saved_run_id", None)

    state = st.session_state.get("sim_tm_state")
    if state:
        _render_target_match_results(state["result"])
        st.divider()
        _render_save_target_run(state, material_profile)

def _render_simulation_result(result, parsed) -> None:
    """Render one execution result — status, parsed values, logs, paths (clearly labelled)."""
    st.markdown(
        f"**{result.scenario_id}** · "
        + app_ui.status_badge(result.status.replace("_", " "),
                              _EXEC_STATUS_LEVEL.get(result.status, "neutral")),
        unsafe_allow_html=True)

    if result.status == phreeqc_executor.STATUS_MISSING:
        st.warning(result.error_message or phreeqc_executor.NOT_CONFIGURED_MESSAGE)
        return
    if result.status in (phreeqc_executor.STATUS_FAILED, phreeqc_executor.STATUS_TIMEOUT):
        st.error(f"PHREEQC did not produce a usable result: {result.error_message}")
        with st.expander("Execution log (stdout / stderr / paths)"):
            if result.stdout_tail:
                st.markdown("**stdout (tail)**")
                st.code(result.stdout_tail, language="text")
            if result.stderr_tail:
                st.markdown("**stderr (tail)**")
                st.code(result.stderr_tail, language="text")
            _render_exec_paths(result)
        return

    # success
    st.success(f"PHREEQC completed in {result.runtime_seconds:.2f}s.")
    st.caption("📌 " + phreeqc_executor.SIM_OUTPUT_LABEL)
    _simulate_provenance_caption()

    if parsed is None:
        st.info("Run succeeded but outputs were not parsed.")
        _render_exec_paths(result)
        return

    cols = st.columns(3)
    cols[0].metric("Predicted pH", "—" if parsed.pH is None else f"{parsed.pH:.2f}")
    cols[1].metric("Predicted pe", "—" if parsed.pe is None else f"{parsed.pe:.2f}")
    cols[2].metric("Elements", len(parsed.element_totals_mM))

    if parsed.element_totals_mM:
        st.markdown("**Predicted dissolved totals** (simulation output)")
        tdf = pd.DataFrame(
            [{"element": el, "mM": round(v, 4)}
             for el, v in sorted(parsed.element_totals_mM.items())])
        st.dataframe(tdf, hide_index=True, use_container_width=True,
                     height=min(280, 60 + 28 * len(tdf)))
        # A graph only because an actual execution result exists.
        st.bar_chart(tdf.set_index("element")["mM"])
        st.caption("Predicted dissolved totals (mM) from PHREEQC execution — a simulation "
                   "output, **not** validated against measured data. This is separate from the "
                   "measured-vs-model pH/residual graphs in **Validate** / **Compare Results**.")

    if parsed.saturation_indices:
        with st.expander(f"Saturation indices ({len(parsed.saturation_indices)})"):
            sidf = pd.DataFrame(parsed.saturation_indices)
            sidf = sidf.reindex(sidf["SI"].abs().sort_values(ascending=False).index)
            st.dataframe(sidf, hide_index=True, use_container_width=True, height=260)

    if parsed.warnings:
        for w in parsed.warnings:
            st.caption("⚠️ " + w)
    if parsed.missing:
        st.caption("Not available from this run: " + ", ".join(parsed.missing))
    _render_exec_paths(result)

def _render_exec_paths(result) -> None:
    with st.expander("Generated files (in the safe simulation workspace)"):
        for label, path in (("input (.pqi)", result.input_path),
                            ("output (.pqo)", result.output_path),
                            ("selected output", result.selected_output_path)):
            st.caption(f"{label}: `{_rel(path)}`" if path else f"{label}: —")
        st.caption(f"PHREEQC: `{result.phreeqc_executable}`  ·  database: "
                   f"`{result.database_path}`  ·  run at {result.timestamp}")

def _render_simulate_tab(selected_run, dev_mode: bool) -> None:
    """Plan a simulation scenario from a plain-language description, then optionally run it.

    Flow: describe → desired outputs → parse (AI if consented, else rule-based) → review
    what was understood + missing/assumptions/warnings → edit/confirm → choose a strategy →
    generate a plan matrix → material profile → PHREEQC input preview → (gated, user-confirmed)
    run + plots + ranking + refinement → save the run. **Generating the plan runs nothing;
    PHREEQC runs only on an explicit confirmed step, no measured data is touched, and every
    output is a simulation prediction — not validated against measured data.**
    """
    st.subheader("Simulate — describe an experiment, plan and run a simulation")
    st.caption(
        "Describe a batch reaction / leaching experiment; the tab converts it into a "
        "structured scenario and a simulation matrix, then — on an explicit, user-confirmed "
        "step (Step 9) — runs PHREEQC and plots the predicted outputs. **Generating the plan "
        "runs nothing, and nothing runs automatically.** Every output here is a **simulation "
        "prediction, not validated** against measured data — validation lives in the "
        "**Compare Results** tab.")

    cfg = ai_config.resolve_config()
    if cfg.enabled:
        st.caption(f"AI extraction is available (model `{cfg.model}`). Tick consent below to use "
                   "it, or parse with rule-based extraction.")
    else:
        st.caption("AI is disabled — the planner will use **rule-based** extraction (low "
                   "confidence). Enable AI in the sidebar **🤖 AI settings** for better extraction.")

    desc = st.text_area(
        "Step 1 — Describe your experiment", key="sim_desc", height=130,
        placeholder=("e.g. I have 2 g of Class C fly ash. I add 10 mL of 0.5 M HCl for 60 "
                     "minutes at room temperature. I centrifuge, filter the liquid, and measure "
                     "pH, Ca, Si, Al and Fe."))
    outputs = st.text_area(
        "Step 2 — Desired variables / outputs", key="sim_outputs", height=70,
        placeholder="e.g. simulate what should be in the liquid and what may have precipitated")

    use_ai = False
    if cfg.enabled:
        st.caption(ai_scenario_parser.SCENARIO_DATA_NOTICE)
        use_ai = st.checkbox(ai_scenario_parser.SCENARIO_CONSENT_LABEL, key="sim_ai_consent")

    if st.button("Step 3 — Parse scenario", key="sim_parse_btn", disabled=not desc.strip()):
        with st.spinner("Extracting scenario…"):
            st.session_state["sim_parse_result"] = ai_scenario_parser.parse_scenario(
                desc, outputs, prefer_ai=use_ai)
        for k in ("sim_matrix", "sim_refinement", "sim_refined_matrix", "sim_ranking"):
            st.session_state.pop(k, None)

    res = st.session_state.get("sim_parse_result")
    if res is None:
        st.info("Enter a description and click **Parse scenario** to begin.")
        return

    # -- Step 4: review ---------------------------------------------------- #
    st.markdown(f"#### Step 4 — Review&nbsp;&nbsp;·&nbsp;&nbsp;parsed by **{res.source_label()}** "
                f"·&nbsp;&nbsp;confidence **{res.confidence:.0%}**")
    if res.used_ai:
        st.caption("Extracted by AI — review every value. AI output is a suggestion, never verified data.")
    else:
        st.caption("Rule-based extraction (no AI) — low confidence; check every value.")
    if res.error:
        st.warning(f"Parser note: {res.error}")

    flat = res.scenario.to_flat_dict()

    def _disp(v):
        # A single string column keeps Streamlit's Arrow serialization happy (mixed
        # float/bool/None/str in one object column otherwise fails to render).
        if v is None:
            return ""
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return str(v)

    overview = pd.DataFrame(
        [{"field": k, "value": _disp(v)}
         for k, v in flat.items() if k not in ("warnings", "confidence")])
    st.markdown("**What the planner understood**")
    st.dataframe(overview, use_container_width=True, height=300, hide_index=True)

    if res.missing:
        st.markdown("**Missing information**")
        for m in res.missing:
            icon = {"error": "🔴", "warning": "🟠"}.get(m.severity, "ℹ️")
            st.markdown(f"- {icon} **{m.label}** — {m.message}")
    if res.assumptions:
        st.markdown("**Assumptions**")
        for a in res.assumptions:
            st.markdown(f"- `{a.field}` = **{a.assumed_value}** — {a.reason} _(source: {a.source})_")
    if res.scenario.warnings:
        st.markdown("**Warnings**")
        for w in res.scenario.warnings:
            st.warning(w)

    st.info(sim_schema.NON_PREDICTION_NOTE)
    st.caption(
        "When this plan is eventually run, results will also depend on the chosen "
        "thermodynamic database (e.g. CEMDATA18) and the candidate-phase list — those model "
        "assumptions affect and limit what a simulation can predict.")

    if dev_mode and res.raw_response:
        with st.expander("Raw AI response (debug — not saved anywhere)"):
            st.code(res.raw_response)

    # -- Step 5: edit / confirm ------------------------------------------- #
    st.markdown("#### Step 5 — Edit / confirm")
    with st.expander("Edit extracted values", expanded=True):
        edited = _simulate_edit_form(flat)
    confirmed = st.checkbox(
        "I have reviewed the extracted scenario, assumptions, and warnings.",
        key="sim_confirm_chk")

    # -- Step 6: choose a simulation strategy, then generate the plan ------ #
    st.markdown("#### Step 6 — Simulation strategy")
    strategy = st.radio(
        "How should the plan be generated?",
        options=[
            "Single scenario (one plan row)",
            "Small parameter sweep",
            "Large batch / design-of-experiments — future",
            "Adaptive (active-learning) search — future",
            "Surrogate-assisted fast search — future",
        ],
        index=0, key="sim_strategy",
        help="Single scenario and small parameter sweeps are supported now. Large-batch, "
             "adaptive, and surrogate-assisted strategies are planned and disabled here.")
    is_future = strategy.endswith("— future")
    ranges = None
    if strategy.startswith("Small parameter sweep"):
        st.caption("Sweep one parameter over a few values (a small Cartesian plan; no "
                   "execution — still plan-only).")
        sweep_field = st.selectbox(
            "Parameter to sweep", list(sim_matrix.RANGEABLE_FIELDS), key="sim_sweep_field")
        sweep_raw = st.text_input(
            "Values (comma-separated)", key="sim_sweep_vals", placeholder="e.g. 0.1, 0.5, 1.0")
        vals = [v for v in (sim_schema.as_float(x) for x in sweep_raw.split(",")) if v is not None]
        if vals:
            ranges = {sweep_field: vals}
            st.caption(f"Plan will have {len(vals)} row(s).")
    if is_future:
        st.info("This strategy is planned for a future version and is disabled here. Use "
                "**Single scenario** or **Small parameter sweep** for now.")

    gen_disabled = (not confirmed) or is_future or (
        strategy.startswith("Small parameter sweep") and not ranges)
    if st.button("Generate simulation plan", key="sim_gen_btn", disabled=gen_disabled):
        sc = sim_schema.SimulationScenario.from_flat_dict(edited)
        sc.liquid_solid_ratio = sc.computed_ls_ratio()
        st.session_state["sim_matrix"] = sim_matrix.build_simulation_matrix(sc, ranges=ranges)
        st.session_state["sim_scenario"] = sc      # confirmed scenario → drives the .pqi preview
        # A fresh, non-refined plan supersedes any prior refinement provenance.
        for k in ("sim_previews", "sim_refinement", "sim_refined_matrix"):
            st.session_state.pop(k, None)
    if not confirmed:
        st.caption("Confirm the reviewed scenario (Step 5) to enable plan generation.")

    mtx = st.session_state.get("sim_matrix")
    if mtx is not None:
        st.success(sim_schema.PLAN_ONLY_LABEL)
        st.info(
            "ℹ️ **Generating this plan runs nothing.** To run the model, use **Step 9 — Run "
            "deterministic model** below (gated and user-confirmed). The **measured-vs-model** "
            "pH / residual graphs in **Validate** and **Compare Results** are driven by "
            "measured data + model results — **changing simulation-plan values never updates "
            "them.**")
        st.dataframe(mtx, use_container_width=True, height=160, hide_index=True)
        st.download_button(
            "Download plan (CSV)", mtx.to_csv(index=False), file_name="simulation_plan.csv",
            mime="text/csv", key="sim_dl_btn")
        st.caption("This plan is **not** a simulation result — deterministic execution is a "
                   "separate, deliberate step the planner never runs for you. (In the current "
                   "fly-ash + PHREEQC workflow, model generation lives in the **Match** tab; "
                   "future backends will run from here.)")
        st.divider()
        selected_profile = _render_material_profile_section(st.session_state.get("sim_scenario"))
        st.divider()
        release_model = _render_release_model_section(st.session_state.get("sim_scenario"),
                                                      selected_profile)
        st.divider()
        phase_template = _render_database_phases_section(st.session_state.get("sim_scenario"))
        st.divider()
        _render_phreeqc_input_preview(st.session_state.get("sim_scenario"), mtx,
                                      material_profile=selected_profile,
                                      dissolution_model=release_model,
                                      phase_template=phase_template)
        st.divider()
        _render_run_deterministic_model(st.session_state.get("sim_previews"), mtx)
        st.divider()
        _render_target_matching(st.session_state.get("sim_scenario"), selected_profile,
                                phase_template, release_model)


# Tab entry point (app.py calls ui.simulate_tab.render).
render = _render_simulate_tab
