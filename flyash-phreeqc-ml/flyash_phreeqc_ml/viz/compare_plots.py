"""Phase 2 plots: measured experimental values vs PHREEQC predictions.

These are *only* produced when measured data exists — the caller (script 05 / the
``make_comparison_plots`` guard) skips them entirely for a blank template, so the
Phase-1 pipeline is never affected.

Two figure types:

* ``measured_vs_phreeqc.png`` — one scatter per analyte (Ca, Si, Al, Fe, pH) with a
  1:1 reference line. Points on the line mean PHREEQC matches the experiment.
* ``residuals_by_sample.png`` — per-sample bars of ``measured - PHREEQC`` for each
  analyte, so over/under-prediction is visible at a glance.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from ..config import RESIDUAL_ELEMENTS  # noqa: E402

# (measured_col, phreeqc_col, residual_col, nice label)
_ANALYTES = [(f"{el}_mM", f"phreeqc_{el}_mM", f"residual_{el}", f"{el} (mM)")
             for el in RESIDUAL_ELEMENTS]
_ANALYTES.append(("final_pH", "phreeqc_pH", "residual_pH", "pH"))


def _has_pairs(comparison: pd.DataFrame, measured_col: str, phreeqc_col: str) -> bool:
    """True if at least one row has both a measured and a PHREEQC value."""
    if measured_col not in comparison.columns or phreeqc_col not in comparison.columns:
        return False
    return bool((comparison[measured_col].notna() & comparison[phreeqc_col].notna()).any())


def make_comparison_plots(
    comparison: pd.DataFrame,
    figures_dir: str | Path,
) -> list[Path]:
    """Create measured-vs-PHREEQC figures. Returns [] if nothing is plottable."""
    figures_dir = Path(figures_dir)
    written: list[Path] = []

    if comparison is None or comparison.empty:
        return written

    plottable = [a for a in _ANALYTES if _has_pairs(comparison, a[0], a[1])]
    if not plottable:
        # Measured data exists but is not yet linked to PHREEQC predictions.
        print("  comparison: no measured/PHREEQC pairs to plot (mapping not set yet).")
        return written

    figures_dir.mkdir(parents=True, exist_ok=True)
    labels = comparison.get("sample_id", pd.Series(range(len(comparison)))).astype(str)

    # --- 1) Scatter measured vs PHREEQC, with 1:1 line -------------------- #
    n = len(plottable)
    ncols = min(3, n)
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.8 * nrows), squeeze=False)
    for idx, (mcol, pcol, _rcol, label) in enumerate(plottable):
        ax = axes[idx // ncols][idx % ncols]
        m = comparison[mcol].astype(float)
        p = comparison[pcol].astype(float)
        ax.scatter(p, m, color="#4878CF", edgecolor="black", linewidth=0.4, zorder=3)
        # 1:1 reference line spanning the data range.
        both = pd.concat([m, p]).dropna()
        if not both.empty:
            lo, hi = float(both.min()), float(both.max())
            pad = (hi - lo) * 0.05 or 1.0
            ax.plot([lo - pad, hi + pad], [lo - pad, hi + pad],
                    color="grey", linestyle="--", linewidth=1, label="1:1")
        ax.set_xlabel(f"PHREEQC {label}", fontsize=8)
        ax.set_ylabel(f"measured {label}", fontsize=8)
        ax.set_title(label, fontsize=10)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7)
    # Hide any unused axes.
    for idx in range(n, nrows * ncols):
        axes[idx // ncols][idx % ncols].axis("off")
    fig.tight_layout()
    out = figures_dir / "measured_vs_phreeqc.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    written.append(out)

    # --- 2) Residuals per sample ------------------------------------------ #
    res_cols = [(rcol, label) for (_m, _p, rcol, label) in plottable
                if rcol in comparison.columns]
    if res_cols:
        n = len(res_cols)
        fig, axes = plt.subplots(n, 1, figsize=(max(6, len(labels) * 0.5), 2.0 * n),
                                 squeeze=False)
        for idx, (rcol, label) in enumerate(res_cols):
            ax = axes[idx][0]
            vals = comparison[rcol].astype(float)
            ax.bar(range(len(vals)), vals.fillna(0).values, color="#6ACC64")
            ax.axhline(0, color="black", linewidth=0.8)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=90, fontsize=6)
            ax.set_title(f"Residual (measured - PHREEQC): {label}", fontsize=10)
            ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        out = figures_dir / "residuals_by_sample.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        written.append(out)

    return written
