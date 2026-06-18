"""Prediction Models — train + use the **ML surrogate** engine (composite mechanical properties).

This is the UI for the ``flyash_phreeqc_ml.ml_models`` engine: curate a training dataset (from the
Evidence Library, with human approval), train a surrogate model (or a clearly-labelled demo
model), read its metrics + model card, and make an *experimental* prediction with an uncertainty
range and applicability warnings.

Honesty contract surfaced here: only **approved** evidence/lab rows train a real model by default;
a model is at most *experimental* (never *validated*); a **demo** model is loudly marked synthetic
and never mixed with real rows unless the user explicitly opts in; predictions are screening
estimates, never measurements. The AI never produces a number — predictions come from the trained
scikit-learn model. PHREEQC is the leaching engine, not the strength engine.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import app_ui
from flyash_phreeqc_ml import run_manager
from flyash_phreeqc_ml.literature import evidence_schema as E
from flyash_phreeqc_ml.literature import evidence_store
from flyash_phreeqc_ml.ml_models import (feature_schema, model_card, model_registry, model_schema,
                                         predict as predict_mod, train, training_data)

# Input fields offered in the prediction form (a usable subset of the full feature schema).
_PREDICT_NUMERIC = ("plastic_dosage_percent", "plastic_particle_size_mm", "water_binder_ratio",
                    "curing_time_days", "curing_temperature_C", "CaO_wt", "SiO2_wt", "Al2O3_wt",
                    "Fe2O3_wt", "activator_concentration_M")
_PREDICT_CATEGORICAL = ("plastic_type", "plastic_form", "fly_ash_class", "activator_type",
                        "specimen_geometry")


# --------------------------------------------------------------------------- #
# Session + path helpers
# --------------------------------------------------------------------------- #
def _rk(run, suffix):
    return f"predmdl_{suffix}__{run or '_none_'}"


def _registry(run):
    return model_registry.ModelRegistry(run_manager.run_outputs_dir(run) / "model_registry")


def _dataset_path(run):
    # Side-effect-free (no mkdir on read); save_dataset creates the parent.
    return run_manager.run_outputs_dir(run) / "model_registry" / "training_data" / "composite.jsonl"


def _evidence_rows(run):
    path = evidence_store.evidence_path(run_manager.run_outputs_dir(run), E.SCHEMA_COMPOSITE)
    return evidence_store.read_evidence(path)


def _dataset(run):
    """The curated training dataset (list[TrainingRow]) — session-cached, seeded from disk."""
    key = _rk(run, "dataset")
    if key not in st.session_state:
        rows = training_data.load_dataset(_dataset_path(run))
        if not rows:                                    # seed from the Evidence Library (pending)
            rows = training_data.rows_from_evidence(_evidence_rows(run))
        st.session_state[key] = rows
    return st.session_state[key]


def _set_dataset(run, rows):
    st.session_state[_rk(run, "dataset")] = rows


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _render_prediction_models(selected_run: str | None, dev_mode: bool = False) -> None:
    app_ui.render_page_header(
        "Prediction Models",
        "Train an experimental ML *surrogate* for composite mechanical properties (compressive "
        "strength first) from approved evidence/lab data, then make screening predictions with an "
        "uncertainty range. The AI never guesses the number — a trained model does; PHREEQC is the "
        "leaching engine, not the strength engine.",
        eyebrow="Curate · train · predict · audit")

    if not selected_run:
        app_ui.render_warning_panel(
            "Select a run", "Choose or create a run in the sidebar — training data and trained "
            "models are stored per run (gitignored). You can still read how the engine works "
            "below.", level="info")
        _render_engine_note()
        return

    if not train.sklearn_available():
        app_ui.render_warning_panel(
            "scikit-learn not installed",
            "The prediction engine needs scikit-learn. Install it with `pip install scikit-learn` "
            "(and scipy). You can still read saved model cards.", level="warning")

    _render_training_data(selected_run)
    st.divider()
    _render_train(selected_run)
    st.divider()
    _render_predict(selected_run)
    st.divider()
    _render_saved_models(selected_run)
    st.divider()
    _render_engine_note()


# --------------------------------------------------------------------------- #
# A. Training data (curate + approve)
# --------------------------------------------------------------------------- #
def _render_training_data(run) -> None:
    app_ui.section_header("Training data",
                          "approved evidence / lab rows only — review before training")
    rows = _dataset(run)
    target = st.session_state.get(_rk(run, "target"), model_schema.DEFAULT_TARGET)
    summary = training_data.summarize_eligibility(rows, target=target)

    app_ui.render_metric_cards([
        {"label": "Rows", "value": summary["n_total"]},
        {"label": "Approved", "value": summary["n_approved"],
         "status": "success" if summary["n_approved"] else "neutral"},
        {"label": "Pending review", "value": summary["n_pending"],
         "status": "warning" if summary["n_pending"] else "neutral"},
        {"label": f"Eligible ({model_schema.target_label(target)})", "value": summary["n_eligible"],
         "caption": "approved + provenance + confidence",
         "status": "success" if summary["n_eligible"] >= train.MIN_REAL_TRAINING_ROWS else "neutral"},
    ])

    c1, c2, c3 = st.columns(3)
    if c1.button("🔄 Sync from Evidence Library", key=_rk(run, "sync"),
                 help="Pull composite evidence rows in (as pending); keeps existing approvals."):
        _sync_from_evidence(run)
        st.rerun()
    if c2.button("💾 Save dataset", key=_rk(run, "save_ds"),
                 help="Persist the curated dataset to this run (gitignored)."):
        training_data.save_dataset(_dataset_path(run), _dataset(run))
        st.success("Saved the training dataset to the run.")
    if c3.button("🗑️ Clear dataset", key=_rk(run, "clear_ds")):
        _set_dataset(run, [])
        st.rerun()

    if not rows:
        st.caption("No training rows yet. Build composite evidence in the **Evidence Library**, "
                   "then **Sync** it here — or add a demo dataset below to test the workflow.")
        if st.button("➕ Add synthetic demo rows (workflow testing only)", key=_rk(run, "seed_demo")):
            _set_dataset(run, training_data.demo_rows())
            st.warning("Added SYNTHETIC demo rows — for workflow testing only, never real data.")
            st.rerun()
        return

    _render_review_editor(run, rows, target)


def _sync_from_evidence(run) -> None:
    """Merge composite evidence rows into the dataset (new rows pending; keep existing approvals)."""
    existing = _dataset(run)
    seen = {r.source_id for r in existing if r.source_id}
    added = 0
    for ev in _evidence_rows(run):
        tr = training_data.from_composite_evidence(ev)
        if tr.source_id and tr.source_id in seen:
            continue
        existing.append(tr)
        if tr.source_id:
            seen.add(tr.source_id)
        added += 1
    _set_dataset(run, existing)
    st.success(f"Synced {added} new evidence row(s) (pending review).")


def _render_review_editor(run, rows, target) -> None:
    """A review table where the user approves/rejects rows before they can train a real model."""
    st.caption("Tick **approve** for rows you trust. Only approved rows with provenance (and, for "
               "literature, sufficient confidence) train a real model by default.")
    display = pd.DataFrame([{
        "approve": r.user_review_status == training_data.REVIEW_APPROVED,
        "source": r.source_type,
        model_schema.target_label(target): training_data.target_value(r, target),
        "plastic": r.plastic_type, "dosage_%": r.plastic_dosage_percent,
        "w/b": r.water_binder_ratio, "curing_d": r.curing_time_days,
        "confidence": round(float(r.extraction_confidence or 0.0), 2),
        "citation": (r.citation or r.source_id or "—"),
    } for r in rows])

    edited = st.data_editor(
        display, use_container_width=True, height=280, hide_index=True,
        disabled=[c for c in display.columns if c != "approve"],
        column_config={"approve": st.column_config.CheckboxColumn("approve", default=False)},
        key=_rk(run, "review_editor"))

    if st.button("✅ Apply review", key=_rk(run, "apply_review")):
        approvals = list(edited["approve"])
        for r, ok in zip(rows, approvals):
            if r.user_review_status != training_data.REVIEW_REJECTED:
                r.user_review_status = (training_data.REVIEW_APPROVED if ok
                                        else training_data.REVIEW_PENDING)
        _set_dataset(run, rows)
        training_data.save_dataset(_dataset_path(run), rows)
        st.success("Applied review and saved.")
        st.rerun()


# --------------------------------------------------------------------------- #
# B. Train
# --------------------------------------------------------------------------- #
def _render_train(run) -> None:
    app_ui.section_header("Train a model")
    c1, c2 = st.columns(2)
    target = c1.selectbox("Target output", list(model_schema.SUPPORTED_TARGETS),
                          format_func=model_schema.target_display, key=_rk(run, "target"))
    model_type = c2.selectbox("Model type", list(model_schema.SUPPORTED_MODEL_TYPES),
                              format_func=lambda m: model_schema.MODEL_TYPE_LABELS[m],
                              key=_rk(run, "mtype"))

    rows = _dataset(run)
    exploratory = st.checkbox(
        "Exploratory mode — include unapproved / low-confidence / demo rows",
        key=_rk(run, "explore"),
        help="Off by default. On, the model trains on rows that are normally excluded; the result "
             "is exploratory only and should never be presented as reliable.")
    kept, excluded = training_data.eligible_rows(
        rows, target=target, allow_unapproved=exploratory,
        min_confidence=(0.0 if exploratory else training_data.DEFAULT_MIN_CONFIDENCE),
        require_provenance=not exploratory, include_demo=exploratory)

    st.caption(f"{len(kept)} eligible row(s) for {model_schema.target_label(target)} "
               f"(need ≥ {train.MIN_REAL_TRAINING_ROWS} for a real model). "
               f"{len(excluded)} excluded.")
    if excluded:
        with app_ui.advanced_expander(f"Why {len(excluded)} row(s) were excluded"):
            st.dataframe(pd.DataFrame(excluded), use_container_width=True, height=180,
                         hide_index=True)

    enough = len(kept) >= train.MIN_REAL_TRAINING_ROWS
    col_a, col_b = st.columns(2)
    if col_a.button("🤖 Train model", key=_rk(run, "train_real"), type="primary",
                    disabled=not (enough and train.sklearn_available())):
        _do_train(run, kept, target, model_type, demo=False, exploratory=exploratory)
    if not enough:
        col_a.caption(f"Not enough approved data to train a reliable model "
                      f"({len(kept)} of {train.MIN_REAL_TRAINING_ROWS}).")

    if col_b.button("🧪 Train DEMO model (synthetic)", key=_rk(run, "train_demo"),
                    disabled=not train.sklearn_available(),
                    help="Trains on synthetic data for workflow testing — never validated, never "
                         "real."):
        _do_train_demo(run, target, model_type)

    _render_last_metrics(run)


def _do_train(run, rows, target, model_type, *, demo, exploratory) -> None:
    try:
        model = train.train_model(rows, target=target, model_type=model_type, demo=demo)
    except train.InsufficientTrainingDataError as exc:
        st.error(exc.message())
        return
    except train.SklearnNotAvailableError as exc:
        st.error(str(exc))
        return
    except train.MLModelError as exc:
        st.error(f"Could not train: {exc}")
        return
    st.session_state[_rk(run, "model")] = model
    tag = " (exploratory)" if exploratory else ""
    st.success(f"Trained {model.display_label()}{tag} on {model.n_train} row(s). "
               "Review the metrics + card, then save it below.")
    st.rerun()


def _do_train_demo(run, target, model_type) -> None:
    try:
        model = train.train_demo_model(target=target, model_type=model_type)
    except train.MLModelError as exc:
        st.error(f"Could not train demo: {exc}")
        return
    st.session_state[_rk(run, "model")] = model
    st.warning("Trained a DEMO model on SYNTHETIC data — for workflow testing only. Its numbers "
               "are meaningless and it must never be presented as validated.")
    st.rerun()


def _render_last_metrics(run) -> None:
    model = st.session_state.get(_rk(run, "model"))
    if model is None:
        return
    with st.container(border=True):
        status = "danger" if model.is_demo else "success"
        app_ui.render_status_badge(model_schema.VALIDATION_LABELS.get(model.validation_status,
                                                                      model.validation_status),
                                   status)
        m = model.metrics
        app_ui.render_metric_cards([
            {"label": "MAE", "value": m.get("MAE"), "caption": model_schema.target_unit(model.target)},
            {"label": "RMSE", "value": m.get("RMSE"), "caption": model_schema.target_unit(model.target)},
            {"label": "R²", "value": (m.get("R2") if m.get("R2") is not None else "n/a"),
             "caption": m.get("method", "")},
            {"label": "Rows", "value": model.n_train, "caption": f"val {model.n_validation}"},
        ])
        if model.is_demo:
            st.error("⚠️ This demo model is for workflow testing only — not validated, not real.")
        else:
            st.caption("Experimental surrogate — cross-validated on the training rows, **not** "
                       "validated against measured experiments.")
        with app_ui.advanced_expander("Model card"):
            st.markdown(model_card.render_markdown(model.card))
        c1, c2 = st.columns(2)
        c1.download_button("⬇️ Export model card (Markdown)",
                           data=model_card.render_markdown(model.card),
                           file_name=f"{model.name}.model_card.md", mime="text/markdown",
                           key=_rk(run, "card_dl"))
        overwrite = c2.checkbox("Overwrite if a model with this name exists",
                                key=_rk(run, "ovr"))
        if c2.button("💾 Save model to registry", key=_rk(run, "save_model")):
            try:
                _registry(run).save(model, overwrite=overwrite)
                st.success(f"Saved '{model.name}' to the run's model registry (gitignored).")
            except model_registry.ModelExistsError:
                st.warning("A model with this name already exists — tick overwrite to replace it.")
            except model_registry.ModelRegistryError as exc:
                st.error(f"Could not save: {exc}")


# --------------------------------------------------------------------------- #
# C. Predict
# --------------------------------------------------------------------------- #
def _available_models(run) -> dict:
    """`{label: ('session'|name)}` of models the user can predict with."""
    out = {}
    session_model = st.session_state.get(_rk(run, "model"))
    if session_model is not None:
        out[f"⟳ current — {session_model.display_label()}"] = ("session", None)
    for rec in _registry(run).list_models():
        demo = rec.get("validation_status") == model_schema.VALIDATION_DEMO
        tag = " · DEMO" if demo else ""
        out[f"{rec.get('name')}{tag}"] = ("registry", rec.get("name"))
    return out


def _resolve_model(run, choice):
    kind, name = choice
    if kind == "session":
        return st.session_state.get(_rk(run, "model"))
    try:
        return _registry(run).load(name)
    except model_registry.ModelRegistryError:
        return None


def _render_predict(run) -> None:
    app_ui.section_header("Predict", "an experimental estimate with uncertainty — not a measurement")
    models = _available_models(run)
    if not models:
        st.caption("Train or save a model first — then predict here.")
        return
    choice_label = st.selectbox("Model", list(models), key=_rk(run, "pick_model"))
    model = _resolve_model(run, models[choice_label])
    if model is None:
        st.warning("Could not load that model.")
        return

    st.caption("Leave a field blank if unknown — blanks are filled with training defaults and "
               "flagged. Inputs outside the training range raise an out-of-domain warning.")
    with st.form(_rk(run, "predict_form")):
        cols = st.columns(3)
        features: dict = {}
        for i, name in enumerate(_PREDICT_NUMERIC):
            txt = cols[i % 3].text_input(feature_schema.feature_label(name), value="",
                                         key=_rk(run, f"pf_{name}"))
            if txt.strip():
                features[name] = txt.strip()
        ccols = st.columns(3)
        for i, name in enumerate(_PREDICT_CATEGORICAL):
            txt = ccols[i % 3].text_input(feature_schema.feature_label(name), value="",
                                          key=_rk(run, f"pc_{name}"))
            if txt.strip():
                features[name] = txt.strip()
        submitted = st.form_submit_button("Predict", type="primary")

    if submitted:
        result = predict_mod.predict(model, features)
        _render_prediction(result)


def _render_prediction(result) -> None:
    with st.container(border=True):
        if result.refused:
            app_ui.render_warning_panel("No prediction", result.refusal_reason or "Cannot predict.",
                                        level="warning")
            return
        status = "danger" if result.is_demo else "warning"
        app_ui.render_status_badge(
            "DEMO — not real" if result.is_demo else "Experimental — not validated", status)
        st.markdown(f"### {result.headline()}")
        if result.interval_text():
            st.caption(f"Approximate 95% range: **{result.interval_text()}** "
                       f"(method: {result.interval_method}, σ≈{result.sigma})")
        st.caption(f"Model: {result.model_name} · v{result.model_version} · "
                   f"trained on {result.n_training_rows} row(s) · source: {result.source_of_model}")
        for w in result.warnings:
            st.caption(f"⚠️ {w}")


# --------------------------------------------------------------------------- #
# D. Saved models
# --------------------------------------------------------------------------- #
def _render_saved_models(run) -> None:
    app_ui.section_header("Saved models", "this run's model registry (gitignored)")
    records = _registry(run).list_models()
    if not records:
        st.caption("No saved models yet.")
        return
    table = [{"name": r.get("name"), "target": r.get("target"),
              "status": r.get("validation_status"), "type": r.get("model_type"),
              "rows": r.get("n_train"), "MAE": (r.get("metrics") or {}).get("MAE"),
              "R2": (r.get("metrics") or {}).get("R2"), "date": r.get("date")}
             for r in records]
    st.dataframe(table, use_container_width=True, height=200, hide_index=True)
    names = [r.get("name") for r in records]
    c1, c2 = st.columns([3, 1])
    to_delete = c1.selectbox("Delete a model", ["—"] + names, key=_rk(run, "del_pick"))
    if c2.button("Delete", key=_rk(run, "del_btn"), disabled=(to_delete == "—")):
        _registry(run).delete(to_delete)
        st.success(f"Deleted '{to_delete}'.")
        st.rerun()


def _render_engine_note() -> None:
    with app_ui.advanced_expander("About this engine"):
        st.markdown(
            "- **What it is:** a data-trained *surrogate* for composite mechanical properties — a "
            "screening estimator, not a validated design value.\n"
            "- **Training data:** approved evidence/lab rows (the **Evidence Library** builds them). "
            "AI-extracted rows are reviewed + confidence-scored before they can train a real model.\n"
            "- **Honesty:** models are *experimental* (cross-validated), never *validated* against "
            "measured experiments; **demo** models use synthetic data and are clearly labelled.\n"
            "- **No AI numbers:** the language model never produces a prediction — a scikit-learn "
            "model does. PHREEQC is the leaching engine, not the strength engine.\n"
            "- **Real validation** still requires measured experimental data (the Validate / Compare "
            "workflow).")


# The app dispatches to ``render``.
render = _render_prediction_models
