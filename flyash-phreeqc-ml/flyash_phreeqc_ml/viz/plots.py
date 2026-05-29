"""Basic exploratory plots for Phase 1.

These are intentionally simple matplotlib figures (no styling dependencies) that
give a first look at the PHREEQC results: element molalities, pH, and the
saturation indices of the carbonation-relevant phases. Every figure is saved as a
PNG into ``reports/figures`` and the list of written paths is returned.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: write files, never open a window
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

# Elements we plot if present (Fe is included for when Fe-bearing runs appear).
_PLOT_ELEMENTS = ["Ca", "Si", "Al", "Fe", "Na", "C"]
_PLOT_PHASES = ["Cal", "Arg", "Portlandite", "CO2(g)", "Qtz", "Gbs"]


def _state_label(row: pd.Series) -> str:
    """Compact x-axis label for one solution state."""
    sol = row.get("solution_number")
    state = str(row.get("state", ""))[:4]
    src = str(row.get("source_file", "")).replace(".pqo", "")
    return f"{src}\nsol{sol}/{state}"


def _bar(ax, labels, values, title, ylabel) -> None:
    ax.bar(range(len(values)), values, color="#4878CF")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=90, fontsize=6)
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=8)
    ax.grid(axis="y", alpha=0.3)


def make_phase1_plots(
    results: pd.DataFrame,
    saturation: pd.DataFrame,
    figures_dir: str | Path,
) -> list[Path]:
    """Create the Phase-1 exploratory figures. Returns written file paths."""
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if results.empty:
        print("  no PHREEQC results to plot.")
        return written

    labels = [_state_label(r) for _, r in results.iterrows()]

    # --- 1) Element molalities (one subplot per element) ------------------- #
    elements = [e for e in _PLOT_ELEMENTS if f"mol_{e}" in results.columns]
    if elements:
        n = len(elements)
        fig, axes = plt.subplots(n, 1, figsize=(max(6, len(labels) * 0.5), 2.4 * n))
        if n == 1:
            axes = [axes]
        for ax, el in zip(axes, elements):
            _bar(ax, labels, results[f"mol_{el}"].fillna(0).values,
                 f"{el} (total molality)", "mol/kgw")
        fig.tight_layout()
        out = figures_dir / "elements_molality.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        written.append(out)

    # --- 2) pH across states ---------------------------------------------- #
    if "pH" in results.columns:
        fig, ax = plt.subplots(figsize=(max(6, len(labels) * 0.5), 3.5))
        _bar(ax, labels, results["pH"].fillna(0).values, "pH by solution state", "pH")
        fig.tight_layout()
        out = figures_dir / "pH.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        written.append(out)

    # --- 3) Saturation indices of key phases ------------------------------ #
    si_cols = [f"SI_{p}" for p in _PLOT_PHASES if f"SI_{p}" in results.columns]
    if si_cols:
        n = len(si_cols)
        fig, axes = plt.subplots(n, 1, figsize=(max(6, len(labels) * 0.5), 2.0 * n))
        if n == 1:
            axes = [axes]
        for ax, col in zip(axes, si_cols):
            phase = col.replace("SI_", "")
            vals = results[col].astype(float)
            ax.bar(range(len(vals)), vals.fillna(0).values, color="#D65F5F")
            ax.axhline(0, color="black", linewidth=0.8)  # SI=0 => equilibrium
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=90, fontsize=6)
            ax.set_title(f"Saturation index: {phase}", fontsize=10)
            ax.set_ylabel("SI", fontsize=8)
            ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        out = figures_dir / "saturation_indices.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        written.append(out)

    return written
