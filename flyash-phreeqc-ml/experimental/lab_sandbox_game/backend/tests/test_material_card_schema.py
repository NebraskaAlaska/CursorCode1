"""Material Card schema validation, plus agreement between schemas.py and material_card_schema.json."""
import json
import os

import schemas


def _good_card():
    return {
        "material_id": "quartz-abc123", "display_name": "Quartz", "data_status": schemas.REFERENCE,
        "formula": "SiO2", "structure_source": schemas.STRUCT_REFERENCE_DB,
        "phases": [{"name": "Quartz", "formula": "SiO2", "source": "reference_database"}],
        "composition": None,
        "allowed_lab_stations": [
            {"station_id": "synthesizer", "eligible": True, "reason": "origin"},
            {"station_id": "xrd", "eligible": True, "reason": "phases + structure present"},
            {"station_id": "phreeqc", "eligible": False, "reason": "no composition"},
            {"station_id": "icp", "eligible": False, "reason": "needs a solution table"},
        ],
        "uncertainty_notes": [], "warnings": [],
    }


def test_complete_card_validates():
    assert schemas.validate_material_card(_good_card()) == []


def test_missing_required_field_is_flagged():
    card = _good_card()
    del card["data_status"]
    problems = schemas.validate_material_card(card)
    assert any("data_status" in p for p in problems)


def test_invalid_data_status_is_flagged():
    card = _good_card()
    card["data_status"] = "totally_real_measured"  # not in the vocabulary
    problems = schemas.validate_material_card(card)
    assert any("data_status" in p for p in problems)


def test_phases_without_structure_source_is_flagged():
    # Phases present but structure_source = none would be an unsupported identification.
    card = _good_card()
    card["structure_source"] = schemas.STRUCT_NONE
    problems = schemas.validate_material_card(card)
    assert any("structure" in p.lower() for p in problems)


def test_xrd_eligible_without_phases_is_flagged():
    card = _good_card()
    card["phases"] = []
    card["structure_source"] = schemas.STRUCT_NONE
    # but still (dishonestly) claim XRD is eligible
    for st in card["allowed_lab_stations"]:
        if st["station_id"] == "xrd":
            st["eligible"] = True
    problems = schemas.validate_material_card(card)
    assert any("XRD" in p for p in problems)


def test_icp_can_never_be_eligible_from_a_card():
    card = _good_card()
    for st in card["allowed_lab_stations"]:
        if st["station_id"] == "icp":
            st["eligible"] = True
    problems = schemas.validate_material_card(card)
    assert any("ICP" in p for p in problems)


def test_json_schema_file_matches_python_vocabulary():
    """material_card_schema.json must enumerate the same data_status / structure_source vocab."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(here, "material_card_schema.json")
    with open(path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    props = spec["properties"]
    assert set(props["data_status"]["enum"]) == set(schemas.DATA_STATUSES)
    assert set(props["structure_source"]["enum"]) == set(schemas.STRUCTURE_SOURCES)
