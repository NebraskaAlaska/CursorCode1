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
from .. import replicates  # noqa: E402  (mapping-status constants)

# (measured_col, phreeqc_col, residual_col, nice label)
_ANALYTES = [(f"{el}_mM", f"phreeqc_{el}_mM", f"residual_{el}", f"{el} (mM)")
             for el in RESIDUAL_ELEMENTS]
_ANALYTES.append(("final_pH", "phreeqc_pH", "residual_pH", "pH"))

# Marker/colour per mapping status, so a scatter visually distinguishes how trust-
# worthy each plotted point is. Unsafe is red (only ever shown when toggled in).
_STATUS_STYLE = {
    replicates.MAPPING_STATUS_EXACT: {"marker": "o", "color": "#4878CF", "label": "exact"},
    replicates.MAPPING_STATUS_SCENARIO: {"marker": "s", "color": "#EE854A",
                                         "label": "scenario-level only"},
    replicates.MAPPING_STATUS_UNSAFE: {"marker": "^", "color": "#D62728", "label": "unsafe"},
    replicates.MAPPING_STATUS_NEEDS_NEW: {"marker": "x", "color": "#7F7F7F",
                                          "label": "needs new simulation"},
}
_DEFAULT_STYLE = {"marker": "o", "color": "#4878CF", "label": "mapped"}


def _style_for(status) -> dict:
    return _STATUS_STYLE.get(status, _DEFAULT_STYLE)


def _has_pairs(comparison: pd.DataFrame, measured_col: str, phreeqc_col: str) -> bool:
    """True if at least one row has both a measured and a PHREEQC value."""
    if measured_col not in comparison.columns or phreeqc_col not in comparison.columns:
        return False
    return bool((comparison[measured_col].notna() & comparison[phreeqc_col].notna()).any())


def comparison_scatter_figure(plotted: pd.DataFrame, variable: str):
    """Live measured-vs-model scatter for one variable, styled by mapping status.

    ``plotted`` is the ``inclusion["plotted"]`` frame (``measured``, ``predicted``,
    ``mapping_status``, ``sample_id``). Points are styled by status (unsafe in red),
    with a 1:1 reference line and a status legend. Returns a matplotlib Figure for the
    app to render; does not write to disk. The *filtering* of which rows appear here is
    decided by :func:`compare.inclusion.comparison_inclusion`, never re-derived.
    """
    fig, ax = plt.subplots(figsize=(6.0, 5.0))
    if plotted is None or plotted.empty:
        ax.text(0.5, 0.5, "No rows plotted for this variable.", ha="center", va="center")
        ax.axis("off")
        return fig

    for status, grp in plotted.groupby("mapping_status"):
        style = _style_for(status)
        ax.scatter(grp["predicted"], grp["measured"], marker=style["marker"],
                   color=style["color"], edgecolor="black", linewidth=0.4,
                   label=style["label"], zorder=3)

    both = pd.concat([plotted["measured"], plotted["predicted"]]).dropna()
    if not both.empty:
        lo, hi = float(both.min()), float(both.max())
        pad = (hi - lo) * 0.05 or 1.0
        ax.plot([lo - pad, hi + pad], [lo - pad, hi + pad],
                color="grey", linestyle="--", linewidth=1, label="1:1")
    ax.set_xlabel(f"model (PHREEQC) {variable}")
    ax.set_ylabel(f"measured {variable}")
    ax.set_title(f"{variable} — measured vs model")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, title="mapping status")
    fig.tight_layout()
    return fig


def corrected_overlay_figure(overlay: pd.DataFrame, variable: str, unit: str = ""):
    """"Corrected (experimental)" overlay: raw PHREEQC, correction + interval, measured.

    ``overlay`` has one row per plotted sample with ``sample_id``, ``measured``,
    ``phreeqc``, ``corrected``, ``corrected_lower``, ``corrected_upper``. The figure
    **always** shows raw PHREEQC and the corrected value (with its 95% interval)
    together — the corrected number is never drawn on its own — so the viewer can see
    how far the correction moves PHREEQC and whether it overshoots the measurement.
    This is a display artifact only; corrected values never feed mapping/validity or
    the comparison CSV.
    """
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    if overlay is None or overlay.empty:
        ax.text(0.5, 0.5, "No rows to overlay.", ha="center", va="center")
        ax.axis("off")
        return fig

    x = list(range(len(overlay)))
    lower = (overlay["corrected"] - overlay["corrected_lower"]).clip(lower=0).to_numpy()
    upper = (overlay["corrected_upper"] - overlay["corrected"]).clip(lower=0).to_numpy()
    ax.errorbar(x, overlay["corrected"], yerr=[lower, upper], fmt="D", color="#8856a7",
                ecolor="#8856a7", elinewidth=1.2, capsize=3, markersize=6, zorder=3,
                label="corrected (experimental) ± 95%")
    ax.scatter(x, overlay["phreeqc"], marker="s", color="#7F7F7F", edgecolor="black",
               linewidth=0.4, zorder=4, label="raw PHREEQC")
    if "measured" in overlay.columns:
        ax.scatter(x, overlay["measured"], marker="o", color="#4878CF", edgecolor="black",
                   linewidth=0.4, zorder=5, label="measured")
    ax.set_xticks(x)
    ax.set_xticklabels(overlay.get("sample_id", pd.Series(x)).astype(str),
                       rotation=90, fontsize=6)
    ylab = f"{variable}" + (f" ({unit})" if unit else "")
    ax.set_ylabel(ylab)
    ax.set_title(f"{variable} — Corrected (experimental): raw PHREEQC + correction + measured")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig


def make_comparison_plots(
    comparison: pd.DataFrame,
    figures_dir: str | Path,
    statuses: dict | None = None,
) -> list[Path]:
    """Create measured-vs-PHREEQC figures. Returns [] if nothing is plottable.

    ``statuses`` (optional) maps ``sample_id -> mapping_status``; when given, the
    scatter points are styled/coloured by status with a status legend (exact vs
    scenario-level vs unsafe). When omitted the plots look exactly as before, so the
    ``scripts/05`` CLI path is unchanged.
    """
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
    # Per-row status (aligned to comparison rows) when a status map is supplied.
    row_status = ([statuses.get(s) for s in labels.tolist()]
                  if statuses else [None] * len(labels))

    # --- 1) Scatter measured vs PHREEQC, with 1:1 line -------------------- #
    n = len(plottable)
    ncols = min(3, n)
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.8 * nrows), squeeze=False)
    for idx, (mcol, pcol, _rcol, label) in enumerate(plottable):
        ax = axes[idx // ncols][idx % ncols]
        m = comparison[mcol].astype(float)
        p = comparison[pcol].astype(float)
        if statuses:
            seen: set = set()
            for status in dict.fromkeys(row_status):  # stable unique order
                mask = [rs == status for rs in row_status]
                style = _style_for(status)
                lab = style["label"] if style["label"] not in seen else None
                seen.add(style["label"])
                ax.scatter(p[mask], m[mask], marker=style["marker"], color=style["color"],
                           edgecolor="black", linewidth=0.4, label=lab, zorder=3)
        else:
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
