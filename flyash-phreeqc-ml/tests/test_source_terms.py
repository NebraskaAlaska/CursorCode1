"""Tests for the material source-term / dissolution layer (``simulation.source_terms``).

Pins: the oxide-wt% → element-moles conversion, the per-element override, the conservative
validation (zero = nothing added; negative rejected; >1 rejected unless allowed), the safe
default (no release), and the builder integration (the .pqi gains a REACTION block + release
comments + the released elements in SELECTED_OUTPUT, while no model preserves the prior
behaviour). PHREEQC is mocked here; one optional real-binary test runs only when configured.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from flyash_phreeqc_ml.materials import profile_schema as MS
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as B
from flyash_phreeqc_ml.simulation import phreeqc_executor as E
from flyash_phreeqc_ml.simulation import source_terms as ST
from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario

REAL_EXE = E._resolve_executable(None)[0]
REAL_DB = E._resolve_database(None)[0]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _profile():
    return MS.MaterialProfile(
        profile_id="t", material_name="Class C fly ash", composition_basis=MS.BASIS_OXIDE_WT,
        entries=MS.parse_composition_text(
            "SiO2 31.1\nAl2O3 18.3\nCaO 19.4\nFe2O3 6.1\nMgO 4.0\nNa2O 1.5\nK2O 0.8\nSO3 2.0"),
        loi_pct=0.4, verification_status=MS.STATUS_USER_CONFIRMED)


def _scenario():
    return SimulationScenario.from_flat_dict(dict(
        material_name="Class C fly ash", solid_mass_g=2.0, liquid_volume_mL=10.0,
        leachant_type="NaOH", leachant_concentration_M=0.5, time_min=60.0, temperature_C=25.0,
        target_elements=["Ca", "Si", "Al", "Fe"]))


def _compute(model, **kw):
    base = dict(material_profile=_profile(), solid_mass_g=2.0, liquid_volume_mL=10.0)
    base.update(kw)
    return ST.compute_source_terms(model, **base)


# --------------------------------------------------------------------------- #
# Conversion + validation
# --------------------------------------------------------------------------- #
def test_no_material_release_is_default_and_adds_nothing():
    res = _compute(ST.no_release())
    assert res.status == ST.STATUS_NO_RELEASE
    assert not res.has_source_terms and not res.reaction_lines
    assert any("no material elements enter" in w.message.lower() for w in res.warnings)


def test_global_release_oxide_to_moles_correct():
    res = _compute(ST.global_release(0.01))           # 1%
    assert res.status == ST.STATUS_RELEASE_INCLUDED
    ca = next(r for r in res.released if r.element == "Ca")
    # CaO 19.4 wt% -> Ca wt% = 19.4 * 0.7147 = 13.865 %; 2 g -> 0.2773 g Ca; /40.078 = 6.919e-3 mol
    assert abs(ca.grams_in_solid - 2.0 * 0.19400 * MS.oxide_gravimetric_factor("CaO")) < 1e-5
    assert abs(ca.moles_released - ca.moles_total * 0.01) < 1e-15
    assert abs(ca.moles_released - 6.919e-5) < 5e-8
    assert ca.oxide == "CaO" and abs(ca.oxide_moles - ca.moles_released) < 1e-15
    # 6.919e-5 mol in 0.01 kg water -> 6.92 mM
    assert abs(ca.concentration_mM - 6.92) < 0.05


def test_al_fe_use_sesquioxide_vehicle():
    res = _compute(ST.global_release(0.01))
    al = next(r for r in res.released if r.element == "Al")
    assert al.oxide == "Al2O3" and abs(al.oxide_moles - al.moles_released / 2) < 1e-15


def test_per_element_overrides_global():
    res = _compute(ST.global_release(0.01, per_element={"Si": 0.005, "Fe": 0.0001}))
    fr = {r.element: r.fraction for r in res.released}
    assert fr["Ca"] == 0.01 and fr["Si"] == 0.005 and fr["Fe"] == 0.0001


def test_zero_release_adds_no_source_terms():
    res = _compute(ST.global_release(0.0))
    assert not res.has_source_terms and not res.reaction_lines


def test_negative_fraction_rejected():
    res = _compute(ST.global_release(-0.1))
    assert any(w.code == "negative_rejected" for w in res.warnings)
    assert not res.has_source_terms


def test_over_unity_rejected_unless_allowed():
    rejected = _compute(ST.global_release(1.5))
    assert any(w.code == "over_unity_rejected" for w in rejected.warnings)
    assert not rejected.has_source_terms
    allowed = _compute(ST.global_release(1.5, allow_over_unity=True))
    assert allowed.status == ST.STATUS_RELEASE_INCLUDED
    assert any(w.code == "over_unity_allowed" for w in allowed.warnings)


def test_missing_or_unconfirmed_profile_blocked():
    assert _compute(ST.global_release(0.01), material_profile=None).status == ST.STATUS_BLOCKED
    draft = _profile()
    draft.verification_status = MS.STATUS_DRAFT
    assert _compute(ST.global_release(0.01), material_profile=draft).status == ST.STATUS_BLOCKED


def test_missing_solid_mass_blocked():
    assert _compute(ST.global_release(0.01), solid_mass_g=None).status == ST.STATUS_BLOCKED


def test_missing_liquid_volume_warns_but_proceeds():
    res = _compute(ST.global_release(0.01), liquid_volume_mL=None)
    assert res.status == ST.STATUS_RELEASE_INCLUDED          # still adds moles
    assert any(w.code == "no_liquid_volume" for w in res.warnings)
    assert all(r.concentration_mM is None for r in res.released)


def test_literature_requires_confirmation():
    unconf = ST.literature_release(0.01, provenance="Smith 2020", confirmed=False)
    assert _compute(unconf).status == ST.STATUS_BLOCKED
    conf = ST.literature_release(0.01, provenance="Smith 2020", confirmed=True)
    res = _compute(conf)
    assert res.status == ST.STATUS_RELEASE_INCLUDED
    assert any("Smith 2020" in a for a in res.assumptions)


def test_measured_liquid_mode():
    res = _compute(ST.measured_liquid({"Ca": 6.9, "Si": 10.0}))
    assert res.status == ST.STATUS_MEASURED_LIQUID
    assert res.solution_extra_lines and "MEASURED" in res.solution_extra_lines[0]
    assert any("measured" in w.message.lower() for w in res.warnings)
    assert any("measured input" in a.lower() for a in res.assumptions)


# --------------------------------------------------------------------------- #
# Builder integration
# --------------------------------------------------------------------------- #
def test_no_model_preserves_prior_behaviour():
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile())
    assert pv.includes_source_terms is False
    assert "REACTION" not in pv.phreeqc_input_text
    assert pv.status == B.STATUS_READY                       # NaOH + usable assay


def test_release_model_adds_reaction_block_and_comments():
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01))
    t = pv.phreeqc_input_text
    assert pv.includes_source_terms is True
    assert "REACTION 1" in t and "CaO" in t and "SiO2" in t
    assert "USER-ASSUMED" in t                               # release-assumption comment
    assert "-water" in t                                     # L/S set so moles -> concentration
    assert any("assumption" in a.lower() for a in pv.assumptions)


def test_selected_output_includes_target_and_released_elements():
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01))
    totals = pv.phreeqc_input_text.split("-totals", 1)[1].splitlines()[0]
    for el in ("Ca", "Si", "Al", "Fe"):
        assert el in totals
    assert "-pe" in pv.phreeqc_input_text


def test_release_with_no_phases_warns_limited_precipitation():
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01))
    assert any("no candidate precipitate phases" in w.lower() or "saturation-index" in w.lower()
               for w in pv.warnings)


# --------------------------------------------------------------------------- #
# Mocked execution: source terms -> parsed nonzero Ca/Si/Al/Fe
# --------------------------------------------------------------------------- #
def test_parsed_output_sees_released_elements_mocked(monkeypatch, tmp_path):
    import pandas as pd
    # a fake batch state carrying the released element molalities
    results = pd.DataFrame([{"state": "batch", "pH": 13.5, "mol_Ca": 6.9e-3, "mol_Si": 1.0e-2,
                             "mol_Al": 7.2e-3, "mol_Fe": 1.5e-3}])
    monkeypatch.setattr(E, "parse_pqo_file", lambda p: ["rec"])
    monkeypatch.setattr(E, "records_to_frames", lambda recs: (results, pd.DataFrame(),
                                                              pd.DataFrame()))
    out = tmp_path / "x.pqo"
    out.write_text("x")
    parsed = E.parse_outputs(E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(out)))
    assert parsed.element_totals_mM["Ca"] > 0 and parsed.element_totals_mM["Fe"] > 0


# --------------------------------------------------------------------------- #
# Optional real PHREEQC: 1% release -> nonzero Ca/Si/Al/Fe
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not (REAL_EXE and REAL_DB),
                    reason="no real PHREEQC binary + database configured")
def test_real_phreeqc_release_gives_nonzero_elements(tmp_path):  # pragma: no cover - env
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01))
    result = E.execute_preview(pv, workdir=tmp_path / "ws")
    assert result.status == E.STATUS_SUCCESS
    parsed = E.parse_outputs(result)
    for el in ("Ca", "Si", "Al", "Fe"):
        assert parsed.element_totals_mM.get(el, 0) > 0
