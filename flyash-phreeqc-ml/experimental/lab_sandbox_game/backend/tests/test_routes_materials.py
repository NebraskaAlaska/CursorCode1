"""Synthesizer honesty: it never invents phases / structure / data for unknown materials."""
import schemas
import routes_materials


def _xrd_eligible(card):
    return any(s["station_id"] == "xrd" and s["eligible"] for s in card["allowed_lab_stations"])


def test_known_formula_gives_reference_phases():
    card = routes_materials.synthesize(formula="SiO2")
    assert card["data_status"] == schemas.REFERENCE
    assert card["structure_source"] == schemas.STRUCT_REFERENCE_DB
    assert card["phases"] and card["phases"][0]["name"] == "Quartz"
    assert _xrd_eligible(card)  # known structure ⇒ XRD plan allowed
    assert schemas.validate_material_card(card) == []


def test_unknown_name_invents_nothing():
    card = routes_materials.synthesize(name="Unobtainium")
    assert card["data_status"] == schemas.UNKNOWN
    assert card["phases"] == []
    assert card["structure_source"] == schemas.STRUCT_NONE
    assert card["composition"] is None
    assert not _xrd_eligible(card)
    assert any("UNKNOWN" in w or "did not parse" in w for w in card["warnings"])
    assert schemas.validate_material_card(card) == []


def test_parseable_but_unknown_formula_is_formula_only():
    # NaCl parses (stoichiometry known) but is not in the catalog → no phases, no structure.
    card = routes_materials.synthesize(formula="NaCl")
    assert card["data_status"] == schemas.FORMULA_ONLY
    assert card["phases"] == []
    assert card["structure_source"] == schemas.STRUCT_NONE
    assert card["composition"]["values"] == {"Na": 1, "Cl": 1}
    assert not _xrd_eligible(card)  # stoichiometry is NOT a phase identity
    assert schemas.validate_material_card(card) == []


def test_user_composition_is_assumed_not_measured():
    card = routes_materials.synthesize(name="My mix",
                                       composition={"SiO2": 34, "CaO": 24, "Al2O3": 18})
    assert card["data_status"] == schemas.ASSUMED
    assert card["composition"]["status"] == schemas.USER_PROVIDED
    assert card["phases"] == []  # a composition is not a phase identity
    assert not _xrd_eligible(card)
    # PHREEQC preview becomes eligible once a composition exists.
    assert any(s["station_id"] == "phreeqc" and s["eligible"] for s in card["allowed_lab_stations"])
    assert schemas.validate_material_card(card) == []


def test_demo_fly_ash_is_labelled_synthetic_demo():
    card = routes_materials.synthesize(name="demo fly ash")
    assert card["data_status"] == schemas.SYNTHETIC_DEMO
    assert card["composition"]["status"] == schemas.SYNTHETIC_DEMO
    assert card["phases"] == []  # amorphous-dominated; no crystalline phase asserted
    assert any("SYNTHETIC DEMO" in w for w in card["warnings"])
    assert schemas.validate_material_card(card) == []


def test_material_id_is_deterministic():
    assert routes_materials.synthesize(formula="SiO2")["material_id"] == \
        routes_materials.synthesize(formula="SiO2")["material_id"]
