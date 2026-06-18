"""Turn training rows into model-ready frames + a scikit-learn preprocessor.

Pure data wrangling on top of pandas/numpy; scikit-learn is imported **lazily** inside
:func:`build_preprocessor` so the rest of the package (and the UI) imports without it. The
preprocessor median-imputes numerics and one-hot-encodes categoricals with
``handle_unknown="ignore"`` so an unseen category at prediction time degrades gracefully (and is
separately flagged as out-of-domain by :mod:`predict`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import feature_schema, model_schema, training_data


def rows_to_frame(rows) -> pd.DataFrame:
    """All feature + target columns as a DataFrame (one row per training row)."""
    cols = list(feature_schema.ALL_FEATURES) + list(model_schema.SUPPORTED_TARGETS)
    records = []
    for r in rows:
        d = r.to_dict() if isinstance(r, training_data.TrainingRow) else dict(r)
        records.append({c: d.get(c) for c in cols})
    return pd.DataFrame(records, columns=cols)


def _present_features(frame: pd.DataFrame, candidate_cols) -> list:
    """Feature columns that carry at least one non-null value (drop all-empty columns)."""
    out = []
    for c in candidate_cols:
        if c in frame.columns and frame[c].notna().any():
            out.append(c)
    return out


def build_xy(rows, target):
    """``(X_df, y, numeric_cols, categorical_cols, n_dropped)`` for ``target``.

    Rows without a numeric target value are dropped (counted in ``n_dropped``). Only feature
    columns with at least one observed value are kept (a constant-NaN column is useless and would
    just be imputed to a constant).
    """
    frame = rows_to_frame(rows)
    if target not in frame.columns:
        raise KeyError(f"unknown target {target!r}")
    y_raw = pd.to_numeric(frame[target], errors="coerce")
    mask = y_raw.notna()
    n_dropped = int((~mask).sum())
    frame = frame.loc[mask].reset_index(drop=True)
    y = y_raw.loc[mask].to_numpy(dtype=float)

    numeric_cols = _present_features(frame, feature_schema.NUMERIC_FEATURES)
    categorical_cols = _present_features(frame, feature_schema.CATEGORICAL_FEATURES)
    x_cols = numeric_cols + categorical_cols
    x_df = frame[x_cols].copy() if x_cols else frame[[]].copy()
    for c in numeric_cols:
        x_df[c] = pd.to_numeric(x_df[c], errors="coerce")
    for c in categorical_cols:
        x_df[c] = x_df[c].astype("object").where(x_df[c].notna(), None)
    return x_df, y, numeric_cols, categorical_cols, n_dropped


def build_preprocessor(numeric_cols, categorical_cols, *, scale_numeric: bool = False):
    """A ``ColumnTransformer`` (median-impute numerics; impute+one-hot categoricals).

    scikit-learn is imported here, lazily. ``scale_numeric`` standardises numerics (useful for the
    Ridge baseline; tree models don't need it).
    """
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    num_steps = [("impute", SimpleImputer(strategy="median"))]
    if scale_numeric:
        num_steps.append(("scale", StandardScaler()))
    numeric_pipe = Pipeline(num_steps)

    # OneHotEncoder kwarg name changed across sklearn versions (sparse → sparse_output).
    try:
        ohe = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:                                            # pragma: no cover - old sklearn
        ohe = OneHotEncoder(handle_unknown="ignore", sparse=False)
    categorical_pipe = Pipeline([
        ("impute", SimpleImputer(strategy="constant", fill_value="unknown")),
        ("onehot", ohe)])

    transformers = []
    if numeric_cols:
        transformers.append(("num", numeric_pipe, list(numeric_cols)))
    if categorical_cols:
        transformers.append(("cat", categorical_pipe, list(categorical_cols)))
    return ColumnTransformer(transformers=transformers, remainder="drop")


def single_row_frame(features: dict, numeric_cols, categorical_cols) -> pd.DataFrame:
    """A one-row frame for prediction (numerics coerced; missing → NaN/None)."""
    data = {}
    for c in numeric_cols:
        v = features.get(c)
        try:
            data[c] = [float(v)] if v is not None and str(v).strip() != "" else [np.nan]
        except (TypeError, ValueError):
            data[c] = [np.nan]
    for c in categorical_cols:
        v = features.get(c)
        data[c] = [None if v is None or str(v).strip() == "" else str(v)]
    return pd.DataFrame(data, columns=list(numeric_cols) + list(categorical_cols))


def feature_coverage(x_df: pd.DataFrame) -> dict:
    """Fraction of non-null values per feature column (0..1)."""
    n = len(x_df)
    if n == 0:
        return {c: 0.0 for c in x_df.columns}
    return {c: round(float(x_df[c].notna().sum()) / n, 3) for c in x_df.columns}


def missingness_summary(x_df: pd.DataFrame) -> dict:
    """Count of missing values per feature column."""
    return {c: int(x_df[c].isna().sum()) for c in x_df.columns}


def feature_ranges(x_df: pd.DataFrame, numeric_cols) -> dict:
    """``{col: [min, max]}`` over observed numeric values (the applicability box)."""
    out = {}
    for c in numeric_cols:
        col = pd.to_numeric(x_df[c], errors="coerce").dropna()
        if len(col):
            out[c] = [float(col.min()), float(col.max())]
    return out


def categories_seen(x_df: pd.DataFrame, categorical_cols) -> dict:
    """``{col: [values]}`` of categorical values seen in training."""
    out = {}
    for c in categorical_cols:
        vals = sorted({str(v) for v in x_df[c].dropna().tolist()})
        if vals:
            out[c] = vals
    return out
