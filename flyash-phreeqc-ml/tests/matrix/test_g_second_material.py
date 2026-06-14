"""MATRIX (g) — CLAIM: the batch-reaction + recovery workflow runs on a SECOND
material (red mud) purely from its profile — closure, (mocked) attribution, and the
recovery-report section all use the material's elements (Ti/V/Fe/Al) and phases
(anatase/…), with the material's precipitate flag — and **zero fly-ash assumptions
leak** (no Ca/Si rows, the flag is the red-mud one, declared Ti assay stays quarantined).
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import (attribution, mass_balance, phreeqc_runner, profiles,
                               replicates, report, units)
from flyash_phreeqc_ml.ml import incompleteness_model as im

M_TI = units.MOLAR_MASSES["Ti"]


def _anatase_selected(mmol_ti=1.0):
    """A mocked PHREEQC selected output: ``mmol_ti`` of Ti precipitated as anatase."""
    return {phreeqc_runner.phase_moles_column("Anatase"): mmol_ti / 1000.0}


# --------------------------------------------------------------------------- #
# Closure — hand-computed Ti moles, off the red-mud profile only
# --------------------------------------------------------------------------- #
def test_red_mud_closure_matches_hand_computed_moles(red_mud_batch_dataset):
    d = red_mud_batch_dataset
    row = d["data"].iloc[0].to_dict()
    c = mass_balance.closure(row, "Ti", profile=d["profile"])
    assert c["status"] == mass_balance.STATUS_COMPLETE
    assert c["n_in"] == pytest.approx(500.0 / M_TI)        # 10 g x 5 wt% -> 500 mg
    assert c["n_liquid"] == pytest.approx(3.0)             # 30 mM x 100 mL / 1000
    assert c["n_solid"] == pytest.approx(240.0 / M_TI)     # 8 g x 3 wt% -> 240 mg
    assert c["gap"] == pytest.approx(500.0 / M_TI - 3.0 - 240.0 / M_TI)
    # Every term carries a real mg->mmol conversion id with the Ti molar mass.
    assert c["provenance"]["n_in"]["molar_mass"] == pytest.approx(M_TI)


# --------------------------------------------------------------------------- #
# Attribution — phases + the precipitate flag come from the red-mud material
# --------------------------------------------------------------------------- #
def test_red_mud_attribution_uses_material_phases_and_flag(red_mud_batch_dataset):
    d = red_mud_batch_dataset
    row = d["data"].iloc[0].to_dict()
    res = attribution.attribute_gap(row, "Ti", _anatase_selected(1.0), profile=d["profile"])
    # The candidate phase is a RED-MUD phase (anatase), not a fly-ash one.
    assert res["by_phase"] == {"Anatase": pytest.approx(1.0)}
    # The material's precipitate_in_measured_solid flag (True) flows through: the
    # precipitate sits in the measured solid, so it explains 0 of the GAP.
    assert res["precipitate_in_measured_solid"] is True
    assert res["gap_explained"] == pytest.approx(0.0)
    assert res["status"] == attribution.STATUS_UNEXPLAINED


# --------------------------------------------------------------------------- #
# Recovery report section — runs on red mud; no fly-ash elements leak
# --------------------------------------------------------------------------- #
def test_red_mud_recovery_section(red_mud_batch_dataset):
    d = red_mud_batch_dataset
    data, profile = d["data"], d["profile"]
    ck = replicates.condition_key(data.iloc[0].to_dict(), profile)
    recs = report._recovery_records(data, profile, selected_outputs={ck: _anatase_selected(1.0)})

    elements = {r["element"] for r in recs}
    assert elements == {"Ti", "V", "Fe", "Al"}             # the material's elements
    assert "Ca" not in elements and "Si" not in elements   # zero fly-ash leak

    ti = next(r for r in recs if r["element"] == "Ti")
    assert ti["n_in"] == pytest.approx(500.0 / M_TI)
    assert ti["gap"] == pytest.approx(500.0 / M_TI - 3.0 - 240.0 / M_TI)
    assert ti["by_phase"] == {"Anatase": pytest.approx(1.0)}
    assert ti["starting_provenance"] == report.CLASS_MEASURED

    # The CSV view has the standard recovery columns and a Ti row.
    table = report._recovery_table(recs)
    assert list(table.columns) == report.RECOVERY_CSV_COLUMNS
    assert (table["element"] == "Ti").any()


# --------------------------------------------------------------------------- #
# Incompleteness training frame — target is the Ti unexplained gap, not a fly-ash one
# --------------------------------------------------------------------------- #
def test_red_mud_incompleteness_dataset(red_mud_batch_dataset):
    d = red_mud_batch_dataset
    data, profile = d["data"], d["profile"]
    ck = replicates.condition_key(data.iloc[0].to_dict(), profile)
    rec = im.build_recovery_dataset(data, profile, selected_outputs={ck: _anatase_selected(1.0)})
    assert im.target_column("Ti") in rec.columns           # unexplained_Ti
    assert im.target_column("Ca") not in rec.columns       # no fly-ash element
    # Precipitate in the measured solid → unexplained == gap (nothing attributed to it).
    closure = mass_balance.closure(data.iloc[0].to_dict(), "Ti", profile=profile)
    assert rec.iloc[0][im.target_column("Ti")] == pytest.approx(closure["gap"])


# --------------------------------------------------------------------------- #
# Quarantine — a literature-PROPOSED declared assay can never be used
# --------------------------------------------------------------------------- #
def test_red_mud_declared_assay_quarantine():
    # Ti is declared literature-proposed → not usable until confirmed; Fe is confirmed.
    assert profiles.usable_declared_assay(profiles.RED_MUD_PROFILE, "Ti") is None
    fe = profiles.usable_declared_assay(profiles.RED_MUD_PROFILE, "Fe")
    assert fe is not None and fe.provenance == profiles.ASSAY_LITERATURE_CONFIRMED
    assert fe.citation and "doi.org" in fe.citation
