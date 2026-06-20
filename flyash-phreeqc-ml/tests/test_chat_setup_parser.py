"""Tests for the deterministic chat → composition / release / database extraction.

The parser transcribes ONLY what the user literally typed (never AI, never invented) into an
unconfirmed DRAFT that must still be explicitly confirmed. These pins cover: the flexible oxide
formats, typo/case handling, the CaO-vs-Ca0 ambiguity, total / impossible-value validation,
assumed units, the release-model + database extraction, and the apply-to-state behaviour
(auto-fill into the canonical agent state, confirmation gating, persistence across turns, and a
display-name change never erasing a confirmed composition).
"""
from __future__ import annotations

import pytest

from flyash_phreeqc_ml.agent import agent_state
from flyash_phreeqc_ml.agent import chat_setup_parser as csp
from flyash_phreeqc_ml.materials import profile_schema as mp
from flyash_phreeqc_ml.simulation import source_terms

DEMO_COMPOSITION = ("use synthetic demo composition sio2 34 al2o3 18 cao 24 fe2o3 7 mgo 5 "
                    "na2o 2 k2o 1 so3 4 loi other 5")


def _species_map(parse):
    return {e.species: e.value for e in parse.entries}


# --------------------------------------------------------------------------- #
# 1) Composition: the one-line demo prompt
# --------------------------------------------------------------------------- #
def test_demo_one_line_composition_parses_all_nine_oxides():
    cp = csp.parse_oxide_composition(DEMO_COMPOSITION)
    assert cp is not None
    sm = _species_map(cp)
    assert sm == {"SiO2": 34.0, "Al2O3": 18.0, "CaO": 24.0, "Fe2O3": 7.0, "MgO": 5.0,
                  "Na2O": 2.0, "K2O": 1.0, "SO3": 4.0, "LOI/Other": 5.0}
    assert cp.total_pct == 100.0
    assert cp.assumed_units is True                       # bare numbers → wt% assumed (confirm)
    assert cp.blocking == []


# --------------------------------------------------------------------------- #
# 2) Composition: flexible human formats all parse
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("composition SiO2 34 wt%, Al2O3 18, CaO 24, Fe2O3 7",
     {"SiO2": 34.0, "Al2O3": 18.0, "CaO": 24.0, "Fe2O3": 7.0}),
    ("sio2=34 al2o3=18 cao=24 fe2o3=7",
     {"SiO2": 34.0, "Al2O3": 18.0, "CaO": 24.0, "Fe2O3": 7.0}),
    ("silica 34%, alumina 18%, calcium oxide 24%",
     {"SiO2": 34.0, "Al2O3": 18.0, "CaO": 24.0}),
    ("SiO2: 34; Al2O3: 18; CaO: 24",
     {"SiO2": 34.0, "Al2O3": 18.0, "CaO": 24.0}),
    ("Class C fly ash has SiO2 34 wt Al2O3 18 wt CaO 24 wt Fe2O3 7 wt",
     {"SiO2": 34.0, "Al2O3": 18.0, "CaO": 24.0, "Fe2O3": 7.0}),
])
def test_flexible_composition_formats(text, expected):
    cp = csp.parse_oxide_composition(text)
    assert cp is not None
    assert _species_map(cp) == expected


# --------------------------------------------------------------------------- #
# 3) Typo + case handling for oxide names
# --------------------------------------------------------------------------- #
def test_typo_and_case_handling():
    cp = csp.parse_oxide_composition("composition AL2O3 18, al203 say no — SIO2 34, Na20 2")
    sm = _species_map(cp)
    # AL2O3 (caps) and al203 (zero-for-O) both → Al2O3 (kept-first 18); SIO2 → SiO2; Na20 → Na2O.
    assert sm["Al2O3"] == 18.0 and sm["SiO2"] == 34.0 and sm["Na2O"] == 2.0


# --------------------------------------------------------------------------- #
# 4) CaO vs Ca0 ambiguity → warned, never silently parsed
# --------------------------------------------------------------------------- #
def test_cao_vs_ca_zero_is_ambiguous_and_warned():
    cp = csp.parse_oxide_composition("composition Ca0 24, SiO2 34, Al2O3 18")
    sm = _species_map(cp)
    assert "CaO" not in sm                                # the ambiguous Ca0 is NOT assumed to be CaO
    assert any("ambiguous" in w.lower() for w in cp.warnings)


# --------------------------------------------------------------------------- #
# 5) Total wt% + impossible-value validation
# --------------------------------------------------------------------------- #
def test_total_pct_and_out_of_range_warning():
    cp = csp.parse_oxide_composition("composition SiO2 10, CaO 5")     # total 15 ≪ 90
    assert cp.total_pct == 15.0
    assert any("total" in w.lower() for w in cp.warnings)


def test_impossible_values_are_flagged_blocking():
    neg = csp.parse_oxide_composition("composition CaO -5, SiO2 34")
    assert any("negative" in b.lower() for b in neg.blocking)
    over = csp.parse_oxide_composition("composition CaO 150, SiO2 34")
    assert any("exceeds 100" in b.lower() for b in over.blocking)


def test_duplicate_oxide_keeps_first_and_warns():
    cp = csp.parse_oxide_composition("composition CaO 24, SiO2 34, CaO 30")
    assert _species_map(cp)["CaO"] == 24.0                # kept first
    assert any("more than once" in w.lower() for w in cp.warnings)


# --------------------------------------------------------------------------- #
# 6) Not a composition → None (no invention)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "I'm leaching Class C fly ash with 0.5 M NaOH and want pH and Ca",
    "leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min at 25 C",
    "measure Si and Al release",
    "",
])
def test_non_composition_text_returns_none(text):
    assert csp.parse_oxide_composition(text) is None


def test_single_oxide_without_cue_is_not_an_assay():
    # One stray "SiO2 2" with no composition cue is too weak to treat as a full assay.
    assert csp.parse_oxide_composition("I want SiO2 2 mM in solution") is None
    # But with a cue word, a single value is accepted.
    assert csp.parse_oxide_composition("oxide composition SiO2 60") is not None


# --------------------------------------------------------------------------- #
# 7) Release model extraction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,frac", [
    ("use global 1 percent release for ca si al fe na k", 0.01),
    ("1% release", 0.01),
    ("global 0.01 release", 0.01),
    ("apply a 5% release fraction", 0.05),
])
def test_global_release_parse(text, frac):
    rp = csp.parse_release(text)
    assert rp is not None and rp.is_global
    assert rp.model.mode == source_terms.MODE_GLOBAL
    assert abs(rp.model.global_fraction - frac) < 1e-9


def test_per_element_release_parse():
    rp = csp.parse_release("release fractions: Ca 2%, Si 1%, Al 0.5%")
    assert rp is not None and not rp.is_global
    assert rp.model.mode == source_terms.MODE_GLOBAL
    assert rp.model.per_element == pytest.approx({"Ca": 0.02, "Si": 0.01, "Al": 0.005})


def test_no_release_word_returns_none():
    assert csp.parse_release("leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min") is None


# --------------------------------------------------------------------------- #
# 8) Database extraction (never a fabricated path)
# --------------------------------------------------------------------------- #
def test_database_parse():
    assert csp.parse_database("use phreeqc.dat and 25 C").name == "phreeqc.dat"
    assert csp.parse_database("use the wateq4f.dat database").name == "wateq4f.dat"
    assert csp.parse_database("leach fly ash with NaOH") is None
    cem = csp.parse_database("use the CEMDATA18 database")
    assert cem.name == "CEMDATA18" and cem.warnings                # carries the not-redistributable note


# --------------------------------------------------------------------------- #
# 9) Confirmation intent detection (distinct from a bare run-confirm)
# --------------------------------------------------------------------------- #
def test_confirmation_detection():
    assert csp.detect_confirmation("I confirm this material composition and the release model")
    assert csp.detect_confirmation("looks good, confirm the composition")
    assert not csp.detect_confirmation("yes")                       # bare → run-confirm, not this
    assert not csp.detect_confirmation("confirm")                   # no subject → not composition
    assert not csp.detect_confirmation("run it")


# --------------------------------------------------------------------------- #
# 10) apply_setup → the canonical agent state (the heart of the sync fix)
# --------------------------------------------------------------------------- #
def test_apply_setup_autofills_draft_into_state():
    s = agent_state.AgentState()
    notes = csp.apply_setup(s, DEMO_COMPOSITION, default_material_name="Class C fly ash")
    assert s.material_profile is not None
    assert s.material_profile.verification_status == mp.STATUS_DRAFT
    assert not s.composition_usable                                # a draft is never usable
    assert len(s.material_profile.entries) == 9
    assert s.material_profile.material_name == "Class C fly ash"
    assert any("draft" in n.lower() for n in notes)


def test_apply_setup_release_and_database_into_state():
    s = agent_state.AgentState()
    csp.apply_setup(s, "use global 1 percent release for ca si al. use phreeqc.dat")
    assert s.release_model.mode == source_terms.MODE_GLOBAL
    assert abs(s.release_model.global_fraction - 0.01) < 1e-9
    assert s.requested_database == "phreeqc.dat"


def test_apply_setup_confirm_flips_draft_when_valid():
    s = agent_state.AgentState()
    csp.apply_setup(s, DEMO_COMPOSITION)                            # draft (total 100)
    assert not s.composition_usable
    csp.apply_setup(s, "I confirm the material composition")
    assert s.composition_usable                                    # now usable
    assert s.material_profile.verification_status == mp.STATUS_USER_CONFIRMED


def test_apply_setup_confirm_blocked_for_impossible_values():
    s = agent_state.AgentState()
    csp.apply_setup(s, "composition CaO 150, SiO2 34")             # impossible single value
    notes = csp.apply_setup(s, "I confirm the composition")
    assert not s.composition_usable                                # confirmation refused
    assert any("can't confirm" in n.lower() or "exceeds 100" in n.lower() for n in notes)


def test_confirmed_composition_persists_and_survives_restatement_and_rename():
    s = agent_state.AgentState()
    csp.apply_setup(s, DEMO_COMPOSITION, default_material_name="Class C fly ash")
    csp.apply_setup(s, "I confirm the composition")
    assert s.composition_usable
    pid = s.material_profile.profile_id

    # A turn with no composition leaves the confirmed profile untouched.
    csp.apply_setup(s, "also measure Si")
    assert s.composition_usable and s.material_profile.profile_id == pid

    # Re-stating the SAME composition (even with a new name) keeps it confirmed (stable identity).
    csp.apply_setup(s, DEMO_COMPOSITION, default_material_name="Class C fly ash type F")
    assert s.composition_usable and s.material_profile.profile_id == pid
    assert s.material_profile.material_name == "Class C fly ash type F"


def test_new_composition_resets_to_draft():
    s = agent_state.AgentState()
    csp.apply_setup(s, DEMO_COMPOSITION)
    csp.apply_setup(s, "I confirm the composition")
    assert s.composition_usable
    # A genuinely different composition must require re-confirmation (never silently usable).
    csp.apply_setup(s, "composition SiO2 50, CaO 30, Al2O3 20")
    assert not s.composition_usable
    assert s.material_profile.verification_status == mp.STATUS_DRAFT


def test_apply_setup_invents_nothing_when_no_composition():
    s = agent_state.AgentState()
    notes = csp.apply_setup(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min")
    assert s.material_profile is None and s.release_model is None
    assert s.requested_database is None
    assert notes == []


def test_parser_emits_no_secret_like_content():
    # The parser/notes never touch keys; a defensive check that nothing key-shaped leaks.
    s = agent_state.AgentState()
    notes = csp.apply_setup(s, DEMO_COMPOSITION + ". use global 1% release. use phreeqc.dat")
    blob = " ".join(notes)
    assert "sk-ant" not in blob and "api_key" not in blob.lower()
