"""Tests for the deterministic PHREEQC input-preview builder (no execution, no AI).

Pins: water / NaOH / HCl drafts; the validation warnings; the quarantine rule (a proposed
assay is never used); unsupported leachant handled safely; the mandatory scenario-metadata
and preview-only disclaimer comments; the matrix sweep; and the hard boundaries — the
builder runs no PHREEQC, imports no AI, and AI modules write no PHREEQC input.
"""
from __future__ import annotations

import ast
from pathlib import Path

import flyash_phreeqc_ml as pkg
from flyash_phreeqc_ml.profiles import AssayValue, MaterialProfile
from flyash_phreeqc_ml.simulation import matrix as sim_matrix
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as B
from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario

PKG_DIR = Path(pkg.__file__).resolve().parent
BUILDER = PKG_DIR / "simulation" / "phreeqc_input_builder.py"


def _scenario(**overrides) -> SimulationScenario:
    """A confirmed-style scenario (fields set as the UI's edit step would)."""
    flat = dict(material_name="Class C fly ash", material_type="class_c_fly_ash",
                solid_mass_g=2.0, liquid_volume_mL=10.0, leachant_type="NaOH",
                leachant_concentration_M=0.5, time_min=60.0, temperature_C=25.0,
                target_elements=["Ca", "Si", "Al", "Fe"])
    flat.update(overrides)
    return SimulationScenario.from_flat_dict(flat)


# --------------------------------------------------------------------------- #
# Per-leachant generation
# --------------------------------------------------------------------------- #
def test_water_preview_generation():
    pv = B.build_phreeqc_input_preview(
        _scenario(leachant_type="deionized water", leachant_concentration_M=None))
    assert pv.template_type == B.TEMPLATE_WATER
    assert "SOLUTION 1" in pv.phreeqc_input_text
    assert "deionized / neutral water" in pv.phreeqc_input_text
    assert not any("concentration" in w for w in pv.warnings)   # water needs no molarity


def test_naoh_preview_generation():
    pv = B.build_phreeqc_input_preview(_scenario(leachant_type="NaOH", leachant_concentration_M=2.0))
    assert pv.template_type == B.TEMPLATE_NAOH
    assert "Na " in pv.phreeqc_input_text and "NaOH" in pv.phreeqc_input_text
    assert any("Na set to the NaOH molarity" in a for a in pv.assumptions)


def test_hcl_preview_generation_is_preview_only():
    pv = B.build_phreeqc_input_preview(_scenario(leachant_type="HCl", leachant_concentration_M=0.5))
    assert pv.template_type == B.TEMPLATE_HCL
    assert "Cl " in pv.phreeqc_input_text
    # the on-demand runner is NaOH-only → HCl flagged preview-only / not validated against it
    assert any("runner" in u.lower() for u in pv.unsupported_features)


# --------------------------------------------------------------------------- #
# Validation warnings (never crashes — returns a labelled draft + warnings)
# --------------------------------------------------------------------------- #
def test_missing_solid_mass_warning():
    pv = B.build_phreeqc_input_preview(_scenario(solid_mass_g=None))
    assert pv.status == B.STATUS_MISSING_FIELD
    assert any("solid mass" in w.lower() for w in pv.warnings)
    assert pv.phreeqc_input_text                       # still produced a draft


def test_missing_liquid_volume_warning():
    pv = B.build_phreeqc_input_preview(_scenario(liquid_volume_mL=None))
    assert pv.status == B.STATUS_MISSING_FIELD
    assert any("liquid volume" in w.lower() for w in pv.warnings)


def test_missing_material_composition_warning():
    # fly ash ships no usable assay → composition required, never invented
    pv = B.build_phreeqc_input_preview(_scenario())
    assert pv.status == B.STATUS_NEEDS_COMPOSITION
    assert any("composition" in w.lower() for w in pv.warnings)
    assert "NOT INCLUDED" in pv.phreeqc_input_text


def test_unsupported_leachant_handled_safely():
    pv = B.build_phreeqc_input_preview(_scenario(leachant_type="citric acid"))
    assert pv.status == B.STATUS_UNSUPPORTED_LEACHANT
    assert pv.template_type == B.TEMPLATE_UNSUPPORTED
    assert pv.phreeqc_input_text                        # no crash; generic placeholder emitted
    assert any("Unsupported leachant" in w for w in pv.warnings)


def test_draft_only_for_generic_material_without_profile():
    pv = B.build_phreeqc_input_preview(
        _scenario(material_name="mystery slag", material_type=None, leachant_type="NaOH"))
    assert pv.status == B.STATUS_DRAFT


def test_ready_for_review_with_usable_assay():
    mat = MaterialProfile(
        material_id="m", display_name="Test material", relevant_elements=("Ca",),
        candidate_phases={"Calcite": "Ca"},
        declared_assay={"Ca": AssayValue(element="Ca", value=20.0, unit="wt%",
                                         provenance="measured")})
    pv = B.build_phreeqc_input_preview(_scenario(leachant_type="NaOH"), material_profile=mat)
    assert pv.status == B.STATUS_READY
    assert "Calcite" in pv.phreeqc_input_text


def test_quarantined_literature_assay_is_not_used():
    mat = MaterialProfile(
        material_id="m", display_name="Lit", relevant_elements=("Ca",),
        declared_assay={"Ca": AssayValue(element="Ca", value=9.0, unit="wt%",
                                         provenance="literature-proposed")})
    pv = B.build_phreeqc_input_preview(_scenario(), material_profile=mat)
    assert pv.status == B.STATUS_NEEDS_COMPOSITION      # proposed assay quarantined → ignored


# --------------------------------------------------------------------------- #
# Content guarantees
# --------------------------------------------------------------------------- #
def test_generated_input_has_scenario_metadata_comments():
    t = B.build_phreeqc_input_preview(_scenario(), scenario_id="SIM-007").phreeqc_input_text
    assert "SIM-007" in t
    assert "Class C fly ash" in t
    for field in ("leachant:", "solid mass", "liquid volume", "reaction time", "temperature"):
        assert field in t


def test_generated_input_has_preview_only_disclaimers():
    t = B.build_phreeqc_input_preview(_scenario()).phreeqc_input_text
    assert "PHREEQC has NOT been run" in t
    assert "DRAFT ONLY" in t
    assert "requires expert review" in t.lower()
    assert "database" in t.lower()
    assert "kinetic" in t.lower()
    assert B.PREVIEW_HEADER_LABEL in t


# --------------------------------------------------------------------------- #
# Matrix path (one preview per row; sweep values reflected)
# --------------------------------------------------------------------------- #
def test_build_previews_for_matrix_one_per_row():
    sc = _scenario(leachant_type="NaOH", leachant_concentration_M=0.5)
    mtx = sim_matrix.build_simulation_matrix(
        sc, ranges={"leachant_concentration_M": [0.1, 0.5, 1.0]})
    previews = B.build_previews_for_matrix(sc, mtx)
    assert len(previews) == 3
    assert [p.scenario_id for p in previews] == ["SIM-001", "SIM-002", "SIM-003"]
    assert "0.1" in previews[0].phreeqc_input_text      # the swept concentration is templated


# --------------------------------------------------------------------------- #
# Hard boundaries
# --------------------------------------------------------------------------- #
def _import_targets(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            out.append(base)
            out += [f"{base}.{a.name}" for a in node.names]
    return out


def test_builder_runs_no_phreeqc_and_imports_no_ai():
    # No import of subprocess/os/phreeqc_runner/AI ⇒ it cannot execute PHREEQC or call AI.
    targets = _import_targets(BUILDER)
    blob = " ".join(targets)
    for bad in ("subprocess", "phreeqc_runner", "import_assist", "scenario_parser",
                "..ai", ".ai.", "compare", "residual", "mapping_table"):
        assert bad not in blob, f"builder must not import {bad!r}"
    assert "os" not in targets and "subprocess" not in targets   # no exec primitives imported


def test_ai_scenario_parser_writes_no_phreeqc_input():
    src = (PKG_DIR / "ai" / "scenario_parser.py").read_text(encoding="utf-8")
    assert "phreeqc_input_builder" not in src
    assert "SOLUTION 1" not in src
