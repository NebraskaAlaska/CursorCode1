"""Build the ``master_dataset.csv`` modeling table.

For Phase 1 the master table is the **PHREEQC results** (one row per simulated
solution state) enriched with the *input* composition of the corresponding
solution, so each row carries both what went in and what came out.

The experimental ICP side is not joined yet: the delivered workbook contains only
mix-design/raw-material data (no measured solution concentrations), so there is no
sound key to join on at this stage. Phase 2 will add that join once measured ICP
results are available. The builder is structured so that adding the join is a
localized change.
"""
from __future__ import annotations

import pandas as pd


def _pivot_input_solutions(input_solutions: pd.DataFrame) -> pd.DataFrame:
    """Turn the long input-solution table into one wide row per (file, solution).

    Element concentrations become ``in_<element>`` columns; scalars (pH, temp,
    units, density) are carried through. The pivot is keyed on
    ``[source_file, solution_number]`` only — ``solution_label`` is *not* used as a
    key because PHREEQC inputs without a label would otherwise be silently dropped
    by ``pivot_table`` (which discards rows with NaN in any index level).
    """
    if input_solutions.empty:
        return pd.DataFrame()

    id_cols = ["source_file", "solution_number"]
    scalar_cols = [
        c
        for c in ["temp", "ph", "pe", "units", "density"]
        if c in input_solutions.columns
    ]

    # One representative label + scalar row per (file, solution).
    label = (
        input_solutions.groupby(id_cols, dropna=False)["solution_label"]
        .first()
        .reset_index()
    )
    scalars = (
        input_solutions.groupby(id_cols, dropna=False)[scalar_cols]
        .first()
        .reset_index()
    )

    # Pivot element concentrations to wide ``in_<element>`` columns.
    elements = input_solutions.dropna(subset=["element"]).copy()
    wide_elements = (
        elements.pivot_table(
            index=id_cols,
            columns="element",
            values="concentration",
            aggfunc="first",
        )
        .add_prefix("in_")
        .reset_index()
    )

    merged = label.merge(scalars, on=id_cols, how="outer").merge(
        wide_elements, on=id_cols, how="outer"
    )
    # Disambiguate input scalar names from PHREEQC-output ones.
    rename = {c: f"in_{c}" for c in scalar_cols}
    return merged.rename(columns=rename)


def build_master_dataset(
    phreeqc_results: pd.DataFrame,
    input_solutions: pd.DataFrame,
) -> pd.DataFrame:
    """Join PHREEQC output results with their input solution compositions.

    The join is on ``solution_number`` only (the input ``.pqi`` and output ``.pqo``
    come from different file names), with a many-to-one merge: every output state
    for solution *N* receives the input composition of solution *N*. When multiple
    input files define the same solution number, the first is used and a note is
    printed.
    """
    if phreeqc_results.empty:
        return phreeqc_results

    master = phreeqc_results.copy()

    inputs_wide = _pivot_input_solutions(input_solutions)
    if not inputs_wide.empty:
        # Collapse to one input row per solution_number (first wins).
        dup = inputs_wide.duplicated(subset=["solution_number"]).sum()
        if dup:
            print(
                f"  note: {dup} duplicate input solution_number(s) across .pqi files; "
                "keeping the first definition of each."
            )
        inputs_by_number = (
            inputs_wide.drop(columns=["source_file"], errors="ignore")
            .drop_duplicates(subset=["solution_number"])
        )
        # Avoid clobbering the output's own solution_label.
        inputs_by_number = inputs_by_number.rename(
            columns={"solution_label": "input_solution_label"}
        )
        master = master.merge(
            inputs_by_number,
            on="solution_number",
            how="left",
        )

    # Stable, readable column order: identifiers first, then everything else.
    id_first = [
        c
        for c in [
            "record_key",
            "source_file",
            "simulation",
            "state",
            "solution_number",
            "solution_label",
            "input_solution_label",
        ]
        if c in master.columns
    ]
    rest = [c for c in master.columns if c not in id_first]
    return master[id_first + rest]
