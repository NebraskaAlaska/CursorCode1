"""Tests for the material composition manager (``flyash_phreeqc_ml.materials``).

Pins the contract the Simulate workflow depends on:

* oxide / element / mg-kg / mol-kg composition resolves to element wt %;
* validation flags negatives (error), implausible oxide sums (warning), and a missing
  material name (error);
* the **trust gate** — a draft / literature-unverified profile is never usable, so the
  PHREEQC input preview stays ``needs_material_composition``; only a *confirmed* profile
  reaches ``ready_for_review`` and adds its composition + source comments to the input;
* nothing is invented and nothing is written to disk.
"""
from __future__ import annotations

from pathlib import Path

import flyash_phreeqc_ml as pkg
from flyash_phreeqc_ml.materials import profile_schema as S
from flyash_phreeqc_ml.materials import profile_validation as V
from flyash_phreeqc_ml.materials import (
    CompositionEntry, CompositionSource, MaterialProfile, parse_composition_text,
    profile_from_literature_candidates, validate_profile)
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as B
from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario

PKG_DIR = Path(pkg.__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _oxide_profile(status=S.STATUS_DRAFT, **kw) -> MaterialProfile:
    entries = parse_composition_text(
        "SiO2 38\nAl2O3 18\nCaO 24\nFe2O3 6\nMgO 5\nNa2O 1.8\nK2O 0.6\nSO3 2.5")
    return MaterialProfile(
        profile_id="fa", material_name="Class C fly ash", material_type="class_c_fly_ash",
        composition_basis=S.BASIS_OXIDE_WT, entries=entries, loi_pct=1.0,
        verification_status=status,
        source=CompositionSource(source_type=S.SOURCE_USER_ENTERED,
                                 source_reference="example XRF"),
        **kw)


def _scenario(**overrides) -> SimulationScenario:
    flat = dict(material_name="Class C fly ash", material_type="class_c_fly_ash",
                solid_mass_g=2.0, liquid_volume_mL=10.0, leachant_type="NaOH",
                leachant_concentration_M=0.5, time_min=60.0, temperature_C=25.0,
                target_elements=["Ca", "Si", "Al", "Fe"])
    flat.update(overrides)
    return SimulationScenario.from_flat_dict(flat)


# --------------------------------------------------------------------------- #
# Conversions / resolution
# --------------------------------------------------------------------------- #
def test_oxide_gravimetric_factors():
    assert abs(S.oxide_gravimetric_factor("CaO") - 0.7147) < 0.002
    assert abs(S.oxide_gravimetric_factor("Fe2O3") - 0.6994) < 0.002
    assert abs(S.oxide_gravimetric_factor("Al2O3") - 0.5293) < 0.002
    assert abs(S.oxide_gravimetric_factor("SiO2") - 0.4674) < 0.002


def test_valid_oxide_profile_resolves_elements():
    p = _oxide_profile()
    res = validate_profile(p)
    assert res.ok and res.can_confirm
    assert res.n_elements_resolved == 8
    assays = p.element_assays()
    # CaO 24 wt% × 0.7147 ≈ 17.15 wt% Ca
    assert abs(assays["Ca"].value - 24.0 * S.oxide_gravimetric_factor("CaO")) < 1e-6
    assert set(assays) == {"Si", "Al", "Ca", "Fe", "Mg", "Na", "K", "S"}


def test_element_and_mgkg_and_molkg_bases():
    el = MaterialProfile(profile_id="e", material_name="m", composition_basis=S.BASIS_ELEMENT_WT,
                         entries=[CompositionEntry("Ca", 17.0)])
    assert abs(el.element_assays()["Ca"].value - 17.0) < 1e-9
    mgkg = MaterialProfile(profile_id="g", material_name="m", composition_basis=S.BASIS_MG_PER_KG,
                           entries=[CompositionEntry("Ca", 170000.0)])   # 17 wt%
    assert abs(mgkg.element_assays()["Ca"].value - 17.0) < 1e-9
    molkg = MaterialProfile(profile_id="o", material_name="m", composition_basis=S.BASIS_MOL_PER_KG,
                            entries=[CompositionEntry("Ca", 1.0)])       # 40.078 g/kg → 4.0078 wt%
    assert abs(molkg.element_assays()["Ca"].value - 40.078 / 10.0) < 1e-6


def test_multiple_species_sum_to_one_element():
    p = MaterialProfile(profile_id="x", material_name="m", composition_basis=S.BASIS_OXIDE_WT,
                        entries=[CompositionEntry("FeO", 3.0), CompositionEntry("Fe2O3", 6.0)])
    fe = p.element_assays()["Fe"].value
    expect = 3.0 * S.oxide_gravimetric_factor("FeO") + 6.0 * S.oxide_gravimetric_factor("Fe2O3")
    assert abs(fe - expect) < 1e-6
    assert "FeO" in p.element_assays()["Fe"].source_species


def test_parser_does_not_read_formula_digit_as_value():
    # the "2" inside SiO2 must not be taken as the value (regression guard)
    e = parse_composition_text("SiO2 38")
    assert (e[0].species, e[0].value) == ("SiO2", 38.0)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_negative_value_is_an_error():
    p = _oxide_profile()
    p.entries.append(CompositionEntry("MnO", -2.0))
    res = validate_profile(p)
    assert not res.ok and not res.can_confirm
    assert any("negative" in e.lower() for e in res.errors)


def test_oxide_sum_below_range_warns():
    p = MaterialProfile(profile_id="lo", material_name="m", composition_basis=S.BASIS_OXIDE_WT,
                        entries=parse_composition_text("SiO2 20\nCaO 10"))   # total 30%
    res = validate_profile(p)
    assert res.oxide_total is not None and res.oxide_total < S.OXIDE_SUM_MIN
    assert any("below" in w.lower() and "%" in w for w in res.warnings)


def test_oxide_sum_above_range_warns():
    p = MaterialProfile(profile_id="hi", material_name="m", composition_basis=S.BASIS_OXIDE_WT,
                        entries=parse_composition_text("SiO2 60\nCaO 60"))   # 120%
    res = validate_profile(p)
    assert any("above" in w.lower() for w in res.warnings)


def test_missing_material_name_is_flagged():
    p = MaterialProfile(profile_id="n", material_name="", composition_basis=S.BASIS_OXIDE_WT,
                        entries=parse_composition_text("CaO 24"))
    res = validate_profile(p)
    assert not res.ok
    assert any("name" in e.lower() for e in res.errors)


def test_unknown_basis_is_an_error():
    p = MaterialProfile(profile_id="b", material_name="m", composition_basis="weird_basis",
                        entries=[CompositionEntry("CaO", 24.0)])
    res = validate_profile(p)
    assert not res.ok and any("basis" in e.lower() for e in res.errors)


def test_unrecognized_species_warns_not_errors():
    p = MaterialProfile(profile_id="u", material_name="m", composition_basis=S.BASIS_OXIDE_WT,
                        entries=parse_composition_text("CaO 24\nUnobtainium 5"))
    res = validate_profile(p)
    assert res.ok                                  # still valid (one good element)
    assert any("unobtainium" in w.lower() for w in res.warnings)


def test_loi_counted_in_total_not_as_element():
    p = MaterialProfile(profile_id="l", material_name="m", composition_basis=S.BASIS_OXIDE_WT,
                        entries=parse_composition_text("SiO2 38\nCaO 24\nLOI 3"))
    assert "C" not in p.element_assays() and "O" not in p.element_assays()
    assert p.oxide_total() == 38 + 24 + 3


# --------------------------------------------------------------------------- #
# Trust gate ↔ PHREEQC input preview
# --------------------------------------------------------------------------- #
def test_draft_profile_not_treated_as_verified():
    p = _oxide_profile(status=S.STATUS_DRAFT)
    assert not p.is_usable
    assert p.usable_assay("Ca") is None                       # gated out
    res = validate_profile(p)
    assert res.usable_for_preview is False
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=p)
    assert pv.status == B.STATUS_NEEDS_COMPOSITION             # draft → composition not used


def test_user_confirmed_profile_feeds_builder_ready():
    p = _oxide_profile(status=S.STATUS_USER_CONFIRMED)
    assert p.is_usable and p.usable_assay("Ca") is not None
    pv = B.build_phreeqc_input_preview(_scenario(leachant_type="NaOH"), material_profile=p)
    assert pv.status == B.STATUS_READY
    assert "Ca " in pv.phreeqc_input_text or "Ca =" in pv.phreeqc_input_text


def test_no_profile_keeps_needs_material_composition():
    # No material profile → fly ash resolves to its (empty) assay → composition not invented.
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=None)
    assert pv.status == B.STATUS_NEEDS_COMPOSITION


def test_preview_includes_material_profile_source_comments():
    p = _oxide_profile(status=S.STATUS_USER_CONFIRMED)
    p.source = CompositionSource(source_type=S.SOURCE_LITERATURE,
                                 citation="https://doi.org/10.x/abc", title="Example assay",
                                 source_reference="ref-1")
    t = B.build_phreeqc_input_preview(_scenario(), material_profile=p).phreeqc_input_text
    assert "composition basis" in t
    assert "composition source" in t
    assert "https://doi.org/10.x/abc" in t
    assert "verification" in t


# --------------------------------------------------------------------------- #
# Literature quarantine
# --------------------------------------------------------------------------- #
def test_literature_profile_requires_confirmation():
    cands = [{"element": "Ca", "value": 17.0, "unit": "wt%",
              "citation": "https://doi.org/10.x/ca", "title": "Some paper", "year": 2020},
             {"element": "Si", "value": 18.0, "unit": "wt%",
              "citation": "https://doi.org/10.x/si", "title": "Some paper", "year": 2020}]
    p = profile_from_literature_candidates("lit", "Class C fly ash", "class_c_fly_ash", cands)
    assert p.verification_status == S.STATUS_LITERATURE_UNVERIFIED
    assert not p.is_usable
    res = validate_profile(p)
    assert res.requires_confirmation and not res.usable_for_preview
    # quarantined → not used by the preview
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=p)
    assert pv.status == B.STATUS_NEEDS_COMPOSITION
    # explicit confirmation makes it usable
    p.verification_status = S.STATUS_USER_CONFIRMED
    assert p.is_usable and p.usable_assay("Ca") is not None
    pv2 = B.build_phreeqc_input_preview(_scenario(), material_profile=p)
    assert pv2.status == B.STATUS_READY


def test_resolved_assay_provenance_is_not_a_science_provenance():
    # A Simulate composition must never be mistakable for a measured science assay.
    from flyash_phreeqc_ml import profiles as sci
    p = _oxide_profile(status=S.STATUS_USER_CONFIRMED)
    av = p.usable_assay("Ca")
    assert av.provenance not in sci.USABLE_ASSAY_PROVENANCE   # "user-confirmed", not "measured"


# --------------------------------------------------------------------------- #
# No I/O — composition manager writes nothing
# --------------------------------------------------------------------------- #
def test_full_flow_writes_no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = _oxide_profile(status=S.STATUS_DRAFT)
    validate_profile(p)
    p.verification_status = S.STATUS_USER_CONFIRMED
    B.build_phreeqc_input_preview(_scenario(), material_profile=p)
    assert list(tmp_path.iterdir()) == []          # nothing written to the run dir / cwd


def test_materials_modules_import_no_io():
    import ast
    for rel in ("materials/profile_schema.py", "materials/profile_validation.py",
                "materials/__init__.py"):
        tree = ast.parse((PKG_DIR / rel).read_text(encoding="utf-8"))
        targets: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets.append(node.module or "")
        for bad in ("os", "subprocess", "pathlib", "io", "json", "yaml", "requests"):
            assert bad not in targets, f"{rel} imports {bad!r} (the manager must do no I/O)"


# --------------------------------------------------------------------------- #
# App render path (the Simulate Step-7 section + Step-8 preview render cleanly)
# --------------------------------------------------------------------------- #
def test_simulate_material_section_renders(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from flyash_phreeqc_ml.ai import scenario_parser as sp
    from flyash_phreeqc_ml.simulation import matrix as M

    monkeypatch.chdir(tmp_path)                     # no run files written during the render
    app_path = PKG_DIR.parent / "app.py"
    sc = _scenario()
    res = sp.parse_scenario("2 g Class C fly ash in 10 mL 0.5 M NaOH for 60 min",
                            "liquid composition", prefer_ai=False)
    p = _oxide_profile(status=S.STATUS_USER_CONFIRMED)
    p.profile_id = "mp1"

    at = AppTest.from_file(str(app_path), default_timeout=60)
    at.run()
    # The Simulate workflow lives in the Workspace section (the assistant is the default).
    at.session_state["nav_section"] = "Workspace"
    at.session_state["sim_parse_result"] = res
    at.session_state["sim_matrix"] = M.build_simulation_matrix(sc)
    at.session_state["sim_scenario"] = sc
    at.session_state["sim_material_profiles"] = {"mp1": p}
    at.session_state["sim_mp_select"] = "mp1"
    at.run()

    assert not at.exception
    mds = " ".join(m.value for m in at.markdown)
    assert "Step 7 — Material profile" in mds
    assert "Step 8 — PHREEQC input preview" in mds
