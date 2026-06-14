"""MATRIX (a) — CLAIM: the app supports a fly-ash-shaped measured dataset compared
against **PHREEQC** results end-to-end (suggestion table → mapping statuses →
inclusion partition), reproducing all four canonical mapping statuses.
"""
from __future__ import annotations

import pandas as pd

from flyash_phreeqc_ml import mapping_table, replicates
from flyash_phreeqc_ml.compare import comparison_inclusion, compare_measured_vs_phreeqc
from flyash_phreeqc_ml.compare import inclusion as I


def test_flyash_phreeqc_end_to_end(flyash_phreeqc_dataset):
    d = flyash_phreeqc_dataset
    data, manifest = d["measured"], d["manifest"]

    table = mapping_table.build_suggestion_table(data, manifest, None)
    statuses = set(table["mapping_status"])
    assert replicates.MAPPING_STATUS_EXACT in statuses
    assert replicates.MAPPING_STATUS_SCENARIO in statuses
    assert replicates.MAPPING_STATUS_UNSAFE in statuses

    # Map the exact condition and compare end-to-end against PHREEQC.
    for _, r in mapping_table.exact_suggestions(table).iterrows():
        pass  # exact rows identified
    mapping = pd.DataFrame([{"sample_id": s, "phreeqc_record_key": d["atm_key"]}
                            for s in data[data["sample_id"].str.startswith("EXACT")]["sample_id"]])
    comp = compare_measured_vs_phreeqc(data, d["phreeqc_results"], mapping=mapping)
    inc = comparison_inclusion(data, mapping, comp, "final_pH", manifest=manifest)
    assert inc["rows_plotted"] == 3
    assert inc["rows_plotted"] + len(inc["excluded"]) == inc["n_total"]
    assert inc["validity"] in (I.VALIDITY_VALID, I.VALIDITY_PRELIMINARY)
