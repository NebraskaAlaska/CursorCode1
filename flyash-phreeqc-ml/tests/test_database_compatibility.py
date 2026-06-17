"""Tests for the PHREEQC database-compatibility + candidate phase-template layer.

PHREEQC is **not** executed here (compatibility is a pure text check) and **no real database
is committed** — tests write tiny fake ``.dat`` text to ``tmp_path``. Coverage: missing-database
handling, family detection, phase presence (present vs absent, ``Calcite`` ≠ ``Cal``), the report
levels, and the builder integration (aqueous-only adds nothing; a template adds only the phases
the configured database defines and lists the rest as warnings/comments).
"""
from __future__ import annotations

from flyash_phreeqc_ml.materials import profile_schema as MS
from flyash_phreeqc_ml.simulation import database_compatibility as DC
from flyash_phreeqc_ml.simulation import phase_templates as PT
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as B
from flyash_phreeqc_ml.simulation import source_terms as ST
from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario


def _fake_db(tmp_path, phases, *, name="test.dat", header="# generic database"):
    """A tiny fake PHREEQC database defining ``phases`` (each name at column 0)."""
    text = header + "\nSOLUTION_MASTER_SPECIES\nPHASES\n" + "".join(
        f"{p}\n\t{p} = {p}\n\tlog_k 0\n" for p in phases)
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _profile():
    return MS.MaterialProfile(
        profile_id="t", material_name="Class C fly ash", composition_basis=MS.BASIS_OXIDE_WT,
        entries=MS.parse_composition_text("SiO2 31\nAl2O3 18\nCaO 19\nFe2O3 6"),
        verification_status=MS.STATUS_USER_CONFIRMED)


def _scenario():
    return SimulationScenario.from_flat_dict(dict(
        material_name="Class C fly ash", solid_mass_g=2.0, liquid_volume_mL=10.0,
        leachant_type="NaOH", leachant_concentration_M=0.5, time_min=60.0, temperature_C=25.0,
        target_elements=["Ca", "Si", "Al", "Fe"]))


# --------------------------------------------------------------------------- #
# Missing database
# --------------------------------------------------------------------------- #
def test_missing_database_handled_safely():
    r = DC.build_report(database="", expected_phases=["Calcite"])
    assert r.database_exists is False
    assert r.compatibility_level == DC.LEVEL_UNKNOWN
    assert r.detected_family == DC.FAMILY_UNKNOWN
    assert r.missing_phases == ["Calcite"] and not r.available_phases
    assert not r.precipitation_meaningful
    assert any("no phreeqc database" in w.lower() for w in r.warnings)


def test_missing_file_path_handled(tmp_path):
    assert DC.read_database_text(str(tmp_path / "nope.dat")) is None
    assert DC.build_report(str(tmp_path / "nope.dat")).database_exists is False


# --------------------------------------------------------------------------- #
# Family detection
# --------------------------------------------------------------------------- #
def test_phreeqc_dat_detected_as_phreeqc_family(tmp_path):
    db = _fake_db(tmp_path, ["Calcite"], name="phreeqc.dat")
    assert DC.detect_family(db) == DC.FAMILY_PHREEQC


def test_cemdata_detected_from_name_and_header(tmp_path):
    by_name = _fake_db(tmp_path, ["Cal", "Portlandite"], name="cemdata18.dat")
    assert DC.detect_family(by_name) == DC.FAMILY_CEMDATA
    by_header = _fake_db(tmp_path, ["Cal"], name="db.dat", header="# CEMDATA18 thermodynamic data")
    assert DC.detect_family(by_header) == DC.FAMILY_CEMDATA


def test_unknown_family_when_no_markers(tmp_path):
    db = _fake_db(tmp_path, ["Calcite"], name="mydata.dat", header="# custom set")
    assert DC.detect_family(db) == DC.FAMILY_UNKNOWN


# --------------------------------------------------------------------------- #
# Phase presence
# --------------------------------------------------------------------------- #
def test_phase_presence_present_and_missing(tmp_path):
    db = _fake_db(tmp_path, ["Calcite", "Gypsum", "Gibbsite"])
    avail = DC.check_phases(["Calcite", "Portlandite", "Gypsum"], db)
    got = {a.phase: a.available for a in avail}
    assert got == {"Calcite": True, "Portlandite": False, "Gypsum": True}


def test_calcite_does_not_match_cal(tmp_path):
    db = _fake_db(tmp_path, ["Calcite"])
    assert DC.database_defines_phases(["Calcite"], db) is True
    assert DC.database_defines_phases(["Cal"], db) is False           # exact-name only


# --------------------------------------------------------------------------- #
# Report levels
# --------------------------------------------------------------------------- #
def test_report_partial_when_some_phases_missing(tmp_path):
    db = _fake_db(tmp_path, ["Calcite", "Gibbsite", "Gypsum"])        # phreeqc.dat-like subset
    r = DC.build_report(db, expected_phases=PT.FLY_ASH_CEMENTITIOUS.phase_names())
    assert set(r.available_phases) == {"Calcite", "Gibbsite", "Gypsum"}
    assert "Portlandite" in r.missing_phases and "Ettringite" in r.missing_phases
    assert r.compatibility_level == DC.LEVEL_PARTIAL
    assert r.precipitation_meaningful


def test_report_suitable_when_all_present(tmp_path):
    db = _fake_db(tmp_path, list(PT.FLY_ASH_CEMENTITIOUS.phase_names()), name="cemdata18.dat")
    r = DC.build_report(db, expected_phases=PT.FLY_ASH_CEMENTITIOUS.phase_names())
    assert not r.missing_phases
    assert r.compatibility_level == DC.LEVEL_SUITABLE


def test_report_basic_aqueous_when_none_present(tmp_path):
    db = _fake_db(tmp_path, ["Quartz"])                              # none of the template phases
    r = DC.build_report(db, expected_phases=PT.RED_MUD.phase_names())
    assert not r.available_phases
    assert r.compatibility_level == DC.LEVEL_BASIC_AQUEOUS
    assert not r.precipitation_meaningful


def test_aqueous_only_template_report_is_basic(tmp_path):
    db = _fake_db(tmp_path, ["Calcite"])
    r = DC.build_report(db, expected_phases=PT.AQUEOUS_ONLY.phase_names())
    assert r.compatibility_level == DC.LEVEL_BASIC_AQUEOUS


# --------------------------------------------------------------------------- #
# Phase templates
# --------------------------------------------------------------------------- #
def test_templates_default_is_aqueous_only():
    assert PT.DEFAULT_TEMPLATE.is_aqueous_only
    assert PT.AQUEOUS_ONLY.phase_names() == []
    assert PT.FLY_ASH_CEMENTITIOUS.phase_names() and not PT.FLY_ASH_CEMENTITIOUS.is_aqueous_only
    assert PT.get_template("does_not_exist").key == PT.DEFAULT_TEMPLATE.key   # safe fallback


# --------------------------------------------------------------------------- #
# Builder integration
# --------------------------------------------------------------------------- #
def _equilibrium_phase_lines(text):
    """The phase names actually added to the EQUILIBRIUM_PHASES block (indented, not comments)."""
    if "EQUILIBRIUM_PHASES 1" not in text:
        return []
    block = text.split("EQUILIBRIUM_PHASES 1", 1)[1].split("SELECTED_OUTPUT", 1)[0]
    return [ln.split()[0] for ln in block.splitlines()
            if ln.startswith("    ") and not ln.strip().startswith("#")]


def test_aqueous_only_adds_no_equilibrium_phases(tmp_path):
    db = _fake_db(tmp_path, ["Calcite", "Gibbsite"])
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01),
                                       phase_template=PT.AQUEOUS_ONLY, database_path=db)
    assert "EQUILIBRIUM_PHASES 1" not in pv.phreeqc_input_text     # the block keyword, not the comment
    assert "Aqueous-only" in pv.phreeqc_input_text
    assert pv.phase_template_key == "aqueous_only"


def test_template_adds_only_available_phases(tmp_path):
    db = _fake_db(tmp_path, ["Calcite", "Gibbsite", "Gypsum"])        # NOT Portlandite/Ettringite
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01),
                                       phase_template=PT.FLY_ASH_CEMENTITIOUS, database_path=db)
    added = _equilibrium_phase_lines(pv.phreeqc_input_text)
    assert set(added) == {"Calcite", "Gibbsite", "Gypsum"}
    # the cement phases the database lacks must NOT be among the ADDED phases
    assert "Portlandite" not in added and "Ettringite" not in added
    assert set(pv.database_report.available_phases) == {"Calcite", "Gibbsite", "Gypsum"}


def test_unavailable_phases_appear_in_warnings_and_comments(tmp_path):
    db = _fake_db(tmp_path, ["Calcite"])
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01),
                                       phase_template=PT.FLY_ASH_CEMENTITIOUS, database_path=db)
    assert any("Portlandite" in w and ("skip" in w.lower() or "does not define" in w.lower())
               for w in pv.warnings)
    assert "SKIPPED" in pv.phreeqc_input_text


def test_preview_comments_include_database_and_template_info(tmp_path):
    db = _fake_db(tmp_path, ["Calcite"], name="phreeqc.dat")
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       phase_template=PT.FLY_ASH_CEMENTITIOUS, database_path=db)
    t = pv.phreeqc_input_text
    assert "database compatibility" in t
    assert "compatibility level" in t
    assert "configured database: phreeqc.dat" in t
    assert "Phase template" in t


def test_no_database_phases_not_added_but_listed(tmp_path):
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01),
                                       phase_template=PT.FLY_ASH_CEMENTITIOUS, database_path="")
    assert "EQUILIBRIUM_PHASES 1" not in pv.phreeqc_input_text   # never add unverified phases
    assert "not added" in pv.phreeqc_input_text.lower()
    assert "Portlandite" in pv.phreeqc_input_text                # still listed as a candidate


def test_no_template_no_phases_default(tmp_path):
    db = _fake_db(tmp_path, ["Calcite"])
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=_profile(),
                                       dissolution_model=ST.global_release(0.01), database_path=db)
    assert "No candidate phases" in pv.phreeqc_input_text
    assert "EQUILIBRIUM_PHASES 1" not in pv.phreeqc_input_text
