"""MATRIX (e) — CLAIM: the generalization layer (Prompt 7 profiles) runs the
grouping → suggestion-table → inclusion chain for an **alternate, non-fly-ash**
dataset at the unit level, using only the profile (no fly-ash columns, no PHREEQC).
"""
from __future__ import annotations

import pandas as pd

from flyash_phreeqc_ml import mapping_table, replicates
from flyash_phreeqc_ml.compare import comparison_inclusion


def test_alternate_profile_chain(alternate_profile_dataset):
    d = alternate_profile_dataset
    data, profile, manifest = d["data"], d["profile"], d["manifest"]

    # Grouping uses the profile's important_fields (treatment + day), no fly-ash logic.
    ann = replicates.annotate(data, profile)
    assert set(ann[replicates.CONDITION_KEY_COLUMN]) == {
        "treatment=WET_day=1", "treatment=DRY_day=1"}

    # Suggestion table: one row per condition; every status is canonical.
    table = mapping_table.build_suggestion_table(data, manifest, None, profile=profile)
    assert len(table) == 2
    assert set(table["mapping_status"]) <= set(replicates.MAPPING_STATUS_DEFINITIONS)

    # Inclusion partitions all rows against the profile's model-prediction column.
    mapping = pd.DataFrame([{"sample_id": s, "phreeqc_record_key": "m1"}
                            for s in data["sample_id"]])
    comp = data.copy()
    comp["phreeqc_record_key"] = "m1"
    comp["model_yield_g"] = 9.0
    inc = comparison_inclusion(data, mapping, comp, "yield_g",
                               manifest=manifest, profile=profile)
    assert inc["n_total"] == 4
    assert inc["rows_plotted"] + len(inc["excluded"]) == inc["n_total"]
