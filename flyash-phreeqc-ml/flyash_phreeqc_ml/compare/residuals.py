"""Measured-vs-PHREEQC residuals (Phase 2 scaffolding).

The residual of interest is ``measured - PHREEQC`` for each analyte:

    residual_Ca = measured_Ca_mM - phreeqc_Ca_mM
    residual_Si = measured_Si_mM - phreeqc_Si_mM
    residual_Al = measured_Al_mM - phreeqc_Al_mM
    residual_Fe = measured_Fe_mM - phreeqc_Fe_mM
    residual_pH = measured_final_pH - phreeqc_pH

These residuals are exactly what a future Phase-3 correction model will learn to
predict ("where PHREEQC disagrees with experiment"). No model is trained here.

Units: PHREEQC reports element totals as molality (mol/kgw); for dilute solutions
that is ~mol/L, so we multiply by 1000 to get mM (matching the lab units). The
factor lives in :data:`config.PHREEQC_MOLALITY_TO_MM`.

Joining measured samples to PHREEQC predictions
-----------------------------------------------
Each measured ``sample_id`` must be linked to the PHREEQC ``record_key`` that
represents the same chemistry. That mapping is experiment-specific and is supplied
explicitly (a dict or a 2-column table), because there is no reliable automatic key
yet. If no mapping is given, the comparison still runs but PHREEQC columns and
residuals are NaN — a deliberate, visible "not linked yet" state rather than a
silent wrong join.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from ..config import PHREEQC_MOLALITY_TO_MM, RESIDUAL_ELEMENTS
from ..instruments import icp_processor as icp_qc


# Columns carried through from the PHREEQC side for context/debugging.
_PHREEQC_CONTEXT_COLS = [
    "record_key",
    "source_file",
    "simulation",
    "state",
    "solution_number",
    "solution_label",
]


def phreeqc_predictions_mM(
    phreeqc_results: pd.DataFrame,
    states: tuple[str, ...] | None = ("batch",),
) -> pd.DataFrame:
    """Build a tidy PHREEQC-prediction table in measured-comparable units.

    Parameters
    ----------
    phreeqc_results:
        The ``phreeqc_results`` frame from Phase 1 (one row per solution state).
    states:
        Which PHREEQC states to keep. Defaults to ``("batch",)`` — the
        post-equilibration result, which is what an experiment measures. Pass
        ``None`` to keep all states.

    Returns a frame with ``phreeqc_record_key``, the context columns, and
    ``phreeqc_<X>_mM`` / ``phreeqc_pH`` columns.
    """
    df = phreeqc_results.copy()
    if states is not None and "state" in df.columns:
        df = df[df["state"].isin(states)]

    out = pd.DataFrame()
    out["phreeqc_record_key"] = df.get("record_key")
    for col in _PHREEQC_CONTEXT_COLS:
        if col in df.columns:
            out[f"phreeqc_{col}" if col != "record_key" else col] = df[col].values

    for el in RESIDUAL_ELEMENTS:
        mol_col = f"mol_{el}"
        if mol_col in df.columns:
            out[f"phreeqc_{el}_mM"] = df[mol_col].values * PHREEQC_MOLALITY_TO_MM
        else:
            out[f"phreeqc_{el}_mM"] = np.nan  # element not modeled in these runs

    out["phreeqc_pH"] = df["pH"].values if "pH" in df.columns else np.nan
    return out.reset_index(drop=True)


def _normalise_mapping(
    measured: pd.DataFrame,
    mapping: Mapping[str, str] | pd.DataFrame | None,
) -> pd.DataFrame:
    """Return *measured* with a ``phreeqc_record_key`` column populated from mapping."""
    measured = measured.copy()

    if mapping is None:
        if "phreeqc_record_key" not in measured.columns:
            measured["phreeqc_record_key"] = np.nan
        return measured

    if isinstance(mapping, pd.DataFrame):
        if not {"sample_id", "phreeqc_record_key"}.issubset(mapping.columns):
            raise ValueError(
                "mapping DataFrame must have columns ['sample_id', 'phreeqc_record_key']"
            )
        measured = measured.merge(
            mapping[["sample_id", "phreeqc_record_key"]], on="sample_id", how="left"
        )
    else:  # dict-like
        measured["phreeqc_record_key"] = measured["sample_id"].map(dict(mapping))

    return measured


def join_measured_to_phreeqc(
    measured: pd.DataFrame,
    predictions: pd.DataFrame,
    mapping: Mapping[str, str] | pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Left-join measured samples to PHREEQC predictions on ``phreeqc_record_key``."""
    measured = _normalise_mapping(measured, mapping)
    predictions = predictions.copy()
    # Coerce the join key to a common (object) dtype on both sides. Without this,
    # an all-NaN key (float64, when no mapping is set) cannot merge against the
    # string record keys.
    measured["phreeqc_record_key"] = measured["phreeqc_record_key"].astype(object)
    predictions["phreeqc_record_key"] = predictions["phreeqc_record_key"].astype(object)
    prediction_counts = predictions["phreeqc_record_key"].value_counts(dropna=False)
    predictions["prediction_mapping_ambiguous"] = predictions["phreeqc_record_key"].map(
        prediction_counts).fillna(0).astype(int) > 1
    joined = measured.merge(predictions, on="phreeqc_record_key", how="left")
    return joined


def compute_residuals(
    joined: pd.DataFrame, *, require_explicit_icp_stage: bool = False,
) -> pd.DataFrame:
    """Add residuals only where both measured and predicted ICP sides pass QC.

    The wide schema carries final corrected ``*_mM`` values.  Active file workflows
    set ``require_explicit_icp_stage=True`` so legacy/missing correction-stage
    provenance fails closed.  Direct callers remain compatible but still receive
    hard numeric, role, unit, element, upstream-QC, and duplicate checks.
    """
    out = icp_qc.annotate_wide_final_concentrations(
        joined.copy(), role=icp_qc.MEASURED,
        elements=RESIDUAL_ELEMENTS,
        require_explicit_stage=require_explicit_icp_stage,
    )
    additions: dict[str, object] = {}
    for element in RESIDUAL_ELEMENTS:
        for col in icp_qc.qc_columns_for(f"phreeqc_{element}_mM").values():
            if col not in out.columns:
                additions[col] = None
        for col, default in (
            (f"residual_{element}", np.nan),
            (f"residual_{element}_validation_eligible", False),
            (f"residual_{element}_qc_reasons", ""),
        ):
            if col not in out.columns:
                additions[col] = default
    if "residual_pH" not in out.columns:
        additions["residual_pH"] = np.nan
    if additions:
        out = pd.concat(
            [out, pd.DataFrame({k: [v] * len(out) for k, v in additions.items()}, index=out.index)],
            axis=1,
        )
    for el in RESIDUAL_ELEMENTS:
        measured_col = f"{el}_mM"
        phreeqc_col = f"phreeqc_{el}_mM"
        residual_col = f"residual_{el}"
        eligible_col = f"{residual_col}_validation_eligible"
        reasons_col = f"{residual_col}_qc_reasons"
        out[residual_col] = np.nan
        out[eligible_col] = False
        out[reasons_col] = ""
        if measured_col not in out.columns or phreeqc_col not in out.columns:
            continue

        pred_qc_cols = icp_qc.qc_columns_for(phreeqc_col)
        for col in pred_qc_cols.values():
            out[col] = out[col].astype(object)

        # Duplicate sample/element measured rows are ambiguous.  Explicit replicates
        # must have distinct sample IDs or use the existing replicate policy.
        if "sample_id" in out.columns:
            supplied = pd.to_numeric(out[measured_col], errors="coerce").notna()
            duplicate_measured = (
                out.loc[supplied, "sample_id"].astype(str).value_counts().to_dict()
            )
        else:
            duplicate_measured = {}

        for idx, source in out.iterrows():
            row = source.to_dict()
            measured_qc = icp_qc.decision_from_wide_row(
                row, measured_col, role=icp_qc.MEASURED, element=el,
                require_explicit_stage=require_explicit_icp_stage,
            )
            predicted_qc = icp_qc.assess_final_concentration(
                row.get(phreeqc_col), role=icp_qc.PREDICTED, element=el,
                unit="mM", stage=icp_qc.FINAL_CORRECTED_STAGE, stage_confirmed=True,
            )
            out.at[idx, pred_qc_cols["role"]] = icp_qc.PREDICTED
            out.at[idx, pred_qc_cols["input_stage"]] = icp_qc.FINAL_CORRECTED_STAGE
            out.at[idx, pred_qc_cols["stage_confirmed"]] = True
            out.at[idx, pred_qc_cols["corrected_value"]] = predicted_qc.numeric_value
            out.at[idx, pred_qc_cols["conversion_authority"]] = icp_qc.CONVERSION_AUTHORITY
            out.at[idx, pred_qc_cols["qc_status"]] = predicted_qc.qc_status
            out.at[idx, pred_qc_cols["qc_codes"]] = "|".join(predicted_qc.qc_codes)
            out.at[idx, pred_qc_cols["qc_reasons"]] = "|".join(predicted_qc.qc_reasons)
            out.at[idx, pred_qc_cols["validation_eligible"]] = predicted_qc.validation_eligible

            reasons = [*measured_qc.qc_reasons, *predicted_qc.qc_reasons]
            sid = str(row.get("sample_id") or "").strip()
            if duplicate_measured.get(sid, 0) > 1:
                reasons.append(
                    f"duplicate measured rows for ({sid}, {el}); explicit replicate mapping/selection required"
                )
            prediction_ambiguous = (
                icp_qc.parse_qc_bool(row.get("prediction_mapping_ambiguous")) is True)
            if prediction_ambiguous:
                reasons.append(
                    f"duplicate predicted rows for mapped key {row.get('phreeqc_record_key')!r}; selection required"
                )
            eligible = (
                measured_qc.validation_eligible and predicted_qc.validation_eligible
                and duplicate_measured.get(sid, 0) <= 1
                and not prediction_ambiguous
            )
            out.at[idx, eligible_col] = bool(eligible)
            out.at[idx, reasons_col] = "|".join(dict.fromkeys(reasons))
            if eligible:
                out.at[idx, residual_col] = measured_qc.numeric_value - predicted_qc.numeric_value

    if "final_pH" in out.columns and "phreeqc_pH" in out.columns:
        measured_ph = pd.to_numeric(out["final_pH"], errors="coerce")
        predicted_ph = pd.to_numeric(out["phreeqc_pH"], errors="coerce")
        finite = np.isfinite(measured_ph) & np.isfinite(predicted_ph)
        out["residual_pH"] = (measured_ph - predicted_ph).where(finite)
    else:
        out["residual_pH"] = np.nan

    return out


def compare_measured_vs_phreeqc(
    measured: pd.DataFrame,
    phreeqc_results: pd.DataFrame,
    mapping: Mapping[str, str] | pd.DataFrame | None = None,
    states: tuple[str, ...] | None = ("batch",),
    *,
    require_explicit_icp_stage: bool = False,
) -> pd.DataFrame:
    """End-to-end: predictions -> join -> residuals. Returns the comparison table."""
    predictions = phreeqc_predictions_mM(phreeqc_results, states=states)
    joined = join_measured_to_phreeqc(measured, predictions, mapping=mapping)
    return compute_residuals(joined, require_explicit_icp_stage=require_explicit_icp_stage)


def predictions_mM_from_manifest(manifest: pd.DataFrame) -> pd.DataFrame:
    """A predictions-in-mM frame built from **any** scenario manifest (model-agnostic).

    The manifest is the canonical intermediate (PHREEQC *or* a generic model produced
    it), so the comparison can be built from it without touching a model-specific
    parser. Maps the manifest's ``predicted_*`` columns onto the comparison's
    ``phreeqc_*`` prediction columns. (The ``phreeqc_`` prefix is the historical
    model-prediction column name kept for backward compatibility — see
    docs/model_prediction_format.md; it does not mean the prediction came from PHREEQC.)
    """
    out = pd.DataFrame()
    if manifest is None or manifest.empty:
        out["phreeqc_record_key"] = []
        return out
    out["phreeqc_record_key"] = manifest.get("phreeqc_record_key")
    out["phreeqc_pH"] = manifest.get("predicted_pH")
    for el in RESIDUAL_ELEMENTS:
        col = f"predicted_{el}_mM"
        out[f"phreeqc_{el}_mM"] = manifest[col].values if col in manifest.columns else np.nan
    for ctx in ("source_file", "state"):
        if ctx in manifest.columns:
            out[f"phreeqc_{ctx}"] = manifest[ctx].values
    return out.reset_index(drop=True)


def compare_measured_to_manifest(
    measured: pd.DataFrame,
    manifest: pd.DataFrame,
    mapping: Mapping[str, str] | pd.DataFrame | None = None,
    *,
    require_explicit_icp_stage: bool = False,
) -> pd.DataFrame:
    """Model-agnostic comparison: measured -> (manifest predictions) -> residuals.

    Identical output shape to :func:`compare_measured_vs_phreeqc`, but built from the
    manifest, so a non-PHREEQC model's predictions compare end-to-end through the same
    residual columns the inclusion logic and plots already consume.
    """
    predictions = predictions_mM_from_manifest(manifest)
    joined = join_measured_to_phreeqc(measured, predictions, mapping=mapping)
    return compute_residuals(joined, require_explicit_icp_stage=require_explicit_icp_stage)
