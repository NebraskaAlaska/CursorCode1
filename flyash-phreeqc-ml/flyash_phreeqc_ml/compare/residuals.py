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
    joined = measured.merge(predictions, on="phreeqc_record_key", how="left")
    return joined


def compute_residuals(joined: pd.DataFrame) -> pd.DataFrame:
    """Add ``residual_<X>`` columns = measured - PHREEQC to a joined frame."""
    out = joined.copy()
    for el in RESIDUAL_ELEMENTS:
        measured_col = f"{el}_mM"
        phreeqc_col = f"phreeqc_{el}_mM"
        if measured_col in out.columns and phreeqc_col in out.columns:
            out[f"residual_{el}"] = out[measured_col] - out[phreeqc_col]
        else:
            out[f"residual_{el}"] = np.nan

    if "final_pH" in out.columns and "phreeqc_pH" in out.columns:
        out["residual_pH"] = out["final_pH"] - out["phreeqc_pH"]
    else:
        out["residual_pH"] = np.nan

    return out


def compare_measured_vs_phreeqc(
    measured: pd.DataFrame,
    phreeqc_results: pd.DataFrame,
    mapping: Mapping[str, str] | pd.DataFrame | None = None,
    states: tuple[str, ...] | None = ("batch",),
) -> pd.DataFrame:
    """End-to-end: predictions -> join -> residuals. Returns the comparison table."""
    predictions = phreeqc_predictions_mM(phreeqc_results, states=states)
    joined = join_measured_to_phreeqc(measured, predictions, mapping=mapping)
    return compute_residuals(joined)


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
) -> pd.DataFrame:
    """Model-agnostic comparison: measured -> (manifest predictions) -> residuals.

    Identical output shape to :func:`compare_measured_vs_phreeqc`, but built from the
    manifest, so a non-PHREEQC model's predictions compare end-to-end through the same
    residual columns the inclusion logic and plots already consume.
    """
    predictions = predictions_mM_from_manifest(manifest)
    joined = join_measured_to_phreeqc(measured, predictions, mapping=mapping)
    return compute_residuals(joined)
