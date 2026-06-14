"""The scenario manifest is the model-agnostic boundary.

Pins the architectural claim behind matrix (f): every module **downstream of the
manifest** (suggestion engine, mapping statuses, inclusion, residuals, plots, bias)
operates on the manifest and must **not** import the PHREEQC ``.pqo`` parser — so a
non-PHREEQC model flows through unchanged. Verified by source inspection (no import)
and by an import-time check that those modules never pull pqo_parser into sys.modules
on their own.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

# Modules that consume the manifest (or its comparison output). None may depend on the
# PHREEQC-specific parser.
DOWNSTREAM_MODULES = [
    "flyash_phreeqc_ml.scenarios",
    "flyash_phreeqc_ml.replicates",
    "flyash_phreeqc_ml.mapping_table",
    "flyash_phreeqc_ml.compare.inclusion",
    "flyash_phreeqc_ml.compare.residuals",
    "flyash_phreeqc_ml.viz.compare_plots",
    "flyash_phreeqc_ml.viz.measured_overview",
    "flyash_phreeqc_ml.ml.residual_stats",
]


@pytest.mark.parametrize("module_name", DOWNSTREAM_MODULES)
def test_downstream_module_does_not_import_pqo_parser(module_name):
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    assert "pqo_parser" not in src, f"{module_name} references the PHREEQC pqo parser"
    assert "parse_pqo" not in src, f"{module_name} references parse_pqo"


def test_manifest_and_downstream_chain_need_no_pqo_parser():
    """The manifest → suggestion → mapping-status → inclusion chain runs on a generic
    model's predictions without importing the PHREEQC parser.

    We build the predictions frame *inline* (the exact shape the generic parser emits)
    so this exercises only the manifest and its consumers — not the parsers package,
    whose ``__init__`` legitimately wires up the PHREEQC parser. After dropping any
    pqo parser module, running the whole chain must not pull it back in.
    """
    import sys
    import pandas as pd

    from flyash_phreeqc_ml import mapping_table, replicates, scenarios
    from flyash_phreeqc_ml.compare import comparison_inclusion, compare_measured_to_manifest

    for name in list(sys.modules):
        if "pqo_parser" in name:
            del sys.modules[name]

    # The generic parser's normalized output shape, constructed without importing it.
    preds = pd.DataFrame([{"record_key": "G1", "model_name": "NotPhreeqc",
                           "predicted_pH": 12.0, "predicted_Ca_mM": 1.0,
                           "leachant": "NaOH", "liquid_solid_ratio": 5,
                           "CO2_condition": "OA"}])
    manifest = scenarios.build_scenario_manifest(preds)
    assert manifest.iloc[0]["phreeqc_record_key"] == "G1"

    measured = pd.DataFrame([{"sample_id": "S1", "leachant": "NaOH", "NaOH_M": "",
                              "CO2_condition": "OA", "liquid_solid_ratio": 5,
                              "final_pH": 12.5}])
    mapping = pd.DataFrame([{"sample_id": "S1", "phreeqc_record_key": "G1"}])
    table = mapping_table.build_suggestion_table(measured, manifest, None)
    assert table.iloc[0]["mapping_status"] in set(replicates.MAPPING_STATUS_DEFINITIONS)
    comp = compare_measured_to_manifest(measured, manifest, mapping)
    inc = comparison_inclusion(measured, mapping, comp, "final_pH", manifest=manifest)
    assert inc["n_total"] == 1

    assert not any("pqo_parser" in m for m in sys.modules), \
        "the manifest + downstream chain pulled in the PHREEQC pqo parser"
