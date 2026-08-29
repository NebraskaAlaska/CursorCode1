"""Phase 1C pins for the deprecated instrument-registry compatibility view."""
from __future__ import annotations

import re
from pathlib import Path

from flyash_phreeqc_ml.instruments import instrument_registry as reg
from flyash_phreeqc_ml.instruments import instrument_schema as schema
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines

EXPECTED_IDS = (
    "phreeqc_leaching_simulator",
    "xrd_advisory",
    "icp_data_processor",
    "ftir_raman_interpreter",
    "sem_eds_processor",
    "tga_dsc_processor",
    "mechanical_testing_processor",
    "ml_surrogate_predictor",
    "literature_evidence_engine",
    "sustainability_cost_screening",
    "experimental_design_assistant",
    "validation_uncertainty_assistant",
)


def test_all_required_instruments_registered():
    assert reg.instrument_ids() == EXPECTED_IDS
    assert len(reg.all_instruments()) == len(reg.instrument_ids()) == 12


def test_registry_metadata_completeness():
    """Every spec carries all required fields with valid mode / execution_mode (none half-described)."""
    problems = [p for spec in reg.all_instruments() for p in schema.validate_spec(spec)]
    assert not problems, "incomplete instrument metadata: " + "; ".join(problems)


def test_modes_and_execution_modes_are_from_the_vocabulary():
    for spec in reg.all_instruments():
        assert schema.is_valid_mode(spec.mode), f"{spec.instrument_id}: bad mode {spec.mode!r}"
        assert schema.is_valid_execution_mode(spec.execution_mode), \
            f"{spec.instrument_id}: bad execution_mode {spec.execution_mode!r}"


def test_registry_is_a_read_only_view_over_the_canonical_contract():
    canonical = machines.list_virtual_lab_machines()
    compatibility = reg.all_instruments()
    assert tuple(spec.canonical_machine for spec in compatibility) == canonical
    assert all(spec.canonical_machine is machine
               for spec, machine in zip(compatibility, canonical, strict=True))
    assert reg.instrument_ids() is not machines.machine_ids()  # equal values, no shared mutable list
    assert reg.instrument_ids() == machines.machine_ids()


def test_deprecated_active_view_is_static_implementation_not_runtime_availability():
    active = reg.active_instruments()
    assert active
    assert all(spec.active == spec.canonical_machine.has_static_implementation for spec in active)
    phreeqc = reg.require(machines.PHREEQC_LEACHING)
    assert phreeqc.active is True
    assert "executable" in " ".join(phreeqc.runtime_requirements).lower()


def test_readiness_is_derived_consistently():
    assert reg.require("phreeqc_leaching_simulator").readiness() == schema.READY
    assert reg.require("icp_data_processor").readiness() == schema.DATA_PROCESSING
    assert reg.require("xrd_advisory").readiness() == schema.ADVISORY
    assert reg.require("ml_surrogate_predictor").readiness() == schema.TRAINED_MODEL_REQUIRED
    assert reg.require("literature_evidence_engine").readiness() == schema.EVIDENCE_REQUIRED
    assert reg.require("ftir_raman_interpreter").readiness() == schema.EVIDENCE_REQUIRED
    assert reg.require("experimental_design_assistant").readiness() == schema.PLANNING
    for spec in reg.all_instruments():
        assert spec.readiness_badge() in ("success", "info", "warning", "neutral")


def test_every_instrument_states_a_limitation_and_safety_note():
    for spec in reg.all_instruments():
        assert spec.limitations, f"{spec.instrument_id} has no limitations"
        assert spec.safety_notes, f"{spec.instrument_id} has no safety notes"


def test_every_compatibility_view_exposes_honest_runtime_requirements():
    for spec in reg.all_instruments():
        assert spec.runtime_requirements
        assert spec.backend_binding


def test_to_dict_is_json_safe_and_lists_readiness():
    d = reg.require("icp_data_processor").to_dict()
    assert d["instrument_id"] == "icp_data_processor"
    assert isinstance(d["required_inputs"], list) and d["required_inputs"]
    assert d["readiness"] == schema.DATA_PROCESSING


def test_legacy_ids_resolve_but_never_escape_as_new_ids():
    aliases = {
        "xrd_advisory_module": "xrd_advisory",
        "mechanical_test_processor": "mechanical_testing_processor",
        "sustainability_screening": "sustainability_cost_screening",
    }
    for legacy, canonical in aliases.items():
        assert reg.require(legacy).instrument_id == canonical
        assert reg.get(legacy) is reg.get(canonical)
        assert reg.display_name(legacy) == reg.display_name(canonical)


def test_unknown_registry_id_fails_explicitly():
    assert reg.get("unrelated-string") is None
    try:
        reg.require("unrelated-string")
    except KeyError as exc:
        assert "unknown instrument" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("unknown registry id did not fail")


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
