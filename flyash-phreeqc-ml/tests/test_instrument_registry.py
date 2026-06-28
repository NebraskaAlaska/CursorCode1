"""Pins for the Digital Lab **instrument registry** + metadata schema.

These keep the registry honest: every instrument is fully described (no half-registered
instruments), the mode / execution-mode vocabularies are respected, exactly the three Phase-1
instruments carry real behavior, readiness is derived consistently, and no instrument's metadata
ever leaks a secret / API key.
"""
from __future__ import annotations

import re
from pathlib import Path

from flyash_phreeqc_ml.instruments import instrument_registry as reg
from flyash_phreeqc_ml.instruments import instrument_schema as schema

EXPECTED_IDS = {
    "phreeqc_leaching_simulator", "icp_data_processor", "xrd_advisory_module",
    "mechanical_test_processor", "ml_surrogate_predictor", "literature_evidence_engine",
    "sustainability_screening", "ftir_raman_interpreter", "sem_eds_processor", "tga_dsc_processor",
}


def test_all_required_instruments_registered():
    ids = set(reg.instrument_ids())
    assert EXPECTED_IDS <= ids, f"missing instruments: {EXPECTED_IDS - ids}"
    assert len(reg.all_instruments()) == len(reg.instrument_ids()) == 10


def test_registry_metadata_completeness():
    """Every spec carries all required fields with valid mode / execution_mode (none half-described)."""
    problems = [p for spec in reg.all_instruments() for p in schema.validate_spec(spec)]
    assert not problems, "incomplete instrument metadata: " + "; ".join(problems)


def test_modes_and_execution_modes_are_from_the_vocabulary():
    for spec in reg.all_instruments():
        assert schema.is_valid_mode(spec.mode), f"{spec.instrument_id}: bad mode {spec.mode!r}"
        assert schema.is_valid_execution_mode(spec.execution_mode), \
            f"{spec.instrument_id}: bad execution_mode {spec.execution_mode!r}"


def test_only_phreeqc_icp_xrd_are_active():
    active = {s.instrument_id for s in reg.active_instruments()}
    assert active == {"phreeqc_leaching_simulator", "icp_data_processor", "xrd_advisory_module"}


def test_readiness_is_derived_consistently():
    assert reg.require("phreeqc_leaching_simulator").readiness() == schema.READY
    assert reg.require("icp_data_processor").readiness() == schema.DATA_PROCESSING
    assert reg.require("xrd_advisory_module").readiness() == schema.ADVISORY
    assert reg.require("ml_surrogate_predictor").readiness() == schema.TRAINED_MODEL_REQUIRED
    assert reg.require("literature_evidence_engine").readiness() == schema.EVIDENCE_REQUIRED
    # advisory-only placeholders → planning
    assert reg.require("ftir_raman_interpreter").readiness() == schema.PLANNING
    for spec in reg.all_instruments():
        assert spec.readiness_badge() in ("success", "info", "warning", "neutral")


def test_every_instrument_states_a_limitation_and_safety_note():
    for spec in reg.all_instruments():
        assert spec.limitations, f"{spec.instrument_id} has no limitations"
        assert spec.safety_notes, f"{spec.instrument_id} has no safety notes"


def test_placeholders_declare_no_executable_engine_in_limitations():
    """The non-active placeholders must say plainly that no engine exists yet (honest UI)."""
    for spec in reg.all_instruments():
        if spec.active:
            continue
        blob = " ".join(spec.limitations).lower()
        assert ("no " in blob and ("engine" in blob or "model" in blob or "library" in blob
                                   or "lca" in blob)) or "not a" in blob or "require" in blob, \
            f"{spec.instrument_id} does not state its limitation clearly: {spec.limitations}"


def test_to_dict_is_json_safe_and_lists_readiness():
    d = reg.require("icp_data_processor").to_dict()
    assert d["instrument_id"] == "icp_data_processor"
    assert isinstance(d["required_inputs"], list) and d["required_inputs"]
    assert d["readiness"] == schema.DATA_PROCESSING


_SECRET_RE = re.compile(r"sk-[A-Za-z0-9]{8,}|api[_-]?key\s*[:=]\s*\S+|secret\s*[:=]\s*\S+", re.I)


def test_no_secret_or_api_key_in_instrument_metadata_or_source():
    # No instrument's metadata strings carry anything key-shaped.
    for spec in reg.all_instruments():
        blob = " ".join([spec.what_it_can_do, *spec.limitations, *spec.safety_notes,
                         *spec.required_inputs, *spec.optional_inputs])
        assert not _SECRET_RE.search(blob), f"{spec.instrument_id} metadata looks secret-bearing"
    # Nor does the package source (defensive; the instruments package never touches keys).
    pkg = Path(reg.__file__).resolve().parent
    for path in pkg.glob("*.py"):
        assert not _SECRET_RE.search(path.read_text(encoding="utf-8")), f"{path.name} looks secret-bearing"
