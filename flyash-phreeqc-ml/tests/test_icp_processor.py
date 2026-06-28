"""Pins for the **ICP Data Processor** — mg/L→mM, dilution, blank, below-detection, residuals,
and the hard refusal to fabricate measured data from a solid composition.

The ICP processor reduces *measured or predicted* solution data. It must convert correctly, apply
corrections in the right order, flag QC problems instead of guessing, build residuals only from
real measured+predicted pairs, and **never** invent measured values from a solid assay alone.
"""
from __future__ import annotations

import re

import pytest

from flyash_phreeqc_ml import units
from flyash_phreeqc_ml.instruments import icp_processor as icp


def _one(rows, **kw):
    return icp.process(rows, **kw).corrected[0]


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #
def test_mgL_to_mM_uses_the_units_registry_molar_mass():
    row = _one([{"sample_id": "S1", "element": "Ca", "concentration": 84, "unit": "mg/L"}])
    assert row.value_mM == pytest.approx(84.0 / units.MOLAR_MASSES["Ca"])
    assert row.conversion_id == "mgL_to_mM"


def test_mM_passes_through_as_identity():
    row = _one([{"sample_id": "S1", "element": "Si", "concentration": 0.8, "unit": "mM"}])
    assert row.value_mM == pytest.approx(0.8)
    assert row.conversion_id == "identity"


def test_rare_earth_elements_are_supported():
    # Sc/La/Ce/Nd/Y were added to the units registry exactly for ICP/REE work.
    for el in ("Mg", "Sc", "La", "Ce", "Nd", "Y"):
        row = _one([{"sample_id": "S", "element": el, "concentration": 100, "unit": "mg/L"}])
        assert row.value_mM == pytest.approx(100.0 / units.MOLAR_MASSES[el])


def test_ppm_and_ppb_convert():
    assert _one([{"sample_id": "S", "element": "Ca", "concentration": 40.078,
                  "unit": "ppm"}]).value_mM == pytest.approx(1.0)
    assert _one([{"sample_id": "S", "element": "Ca", "concentration": 40078.0,
                  "unit": "ppb"}]).value_mM == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Corrections
# --------------------------------------------------------------------------- #
def test_dilution_correction_multiplies_before_conversion():
    row = _one([{"sample_id": "S1", "element": "Ca", "concentration": 84, "unit": "mg/L",
                 "dilution_factor": 10}])
    assert row.corrected_value == pytest.approx(840.0)
    assert row.value_mM == pytest.approx(840.0 / units.MOLAR_MASSES["Ca"])


def test_blank_correction_subtracts_the_blank():
    row = _one([{"sample_id": "S1", "element": "Ca", "concentration": 10.0, "unit": "mM",
                 "blank_value": 0.5}])
    assert row.blank_corrected_value == pytest.approx(9.5)
    assert row.value_mM == pytest.approx(9.5)


def test_blank_correction_can_be_disabled():
    row = _one([{"sample_id": "S1", "element": "Ca", "concentration": 10.0, "unit": "mM",
                 "blank_value": 0.5}], apply_blank=False)
    assert row.value_mM == pytest.approx(10.0)


def test_blank_above_signal_clamps_to_zero_and_warns():
    res = icp.process([{"sample_id": "S1", "element": "Ca", "concentration": 0.2, "unit": "mM",
                        "blank_value": 0.5}])
    assert res.corrected[0].blank_corrected_value == 0.0
    assert any("below blank" in w or "≥ reading" in w for w in res.warnings)


def test_below_detection_limit_is_flagged():
    row = _one([{"sample_id": "S1", "element": "Si", "concentration": 0.05, "unit": "mg/L",
                 "detection_limit": 0.1}])
    assert row.below_detection_limit is True


def test_blank_and_dilution_compose_in_order():
    # (84 - 0.5) * 10 then /M_Ca
    row = _one([{"sample_id": "S1", "element": "Ca", "concentration": 84.0, "unit": "mg/L",
                 "blank_value": 0.5, "dilution_factor": 10}])
    assert row.corrected_value == pytest.approx((84.0 - 0.5) * 10)
    assert row.value_mM == pytest.approx((84.0 - 0.5) * 10 / units.MOLAR_MASSES["Ca"])


# --------------------------------------------------------------------------- #
# QC warnings (flag, never guess)
# --------------------------------------------------------------------------- #
def test_missing_unit_warns_and_does_not_convert():
    res = icp.process([{"sample_id": "S1", "element": "Ca", "concentration": 84}])
    assert res.corrected[0].value_mM is None
    assert any("missing unit" in w for w in res.warnings)


def test_unknown_element_warns_and_does_not_convert():
    res = icp.process([{"sample_id": "S1", "element": "Xx", "concentration": 1.0, "unit": "mg/L"}])
    assert res.corrected[0].value_mM is None
    assert any("unknown element" in w.lower() for w in res.warnings)


def test_negative_value_is_flagged_impossible():
    res = icp.process([{"sample_id": "S1", "element": "Ca", "concentration": -3, "unit": "mg/L"}])
    assert any("negative" in w and "impossible" in w for w in res.warnings)


def test_missing_dilution_factor_defaults_to_one():
    row = _one([{"sample_id": "S1", "element": "Ca", "concentration": 84, "unit": "mg/L"}])
    assert row.dilution_factor == 1.0


# --------------------------------------------------------------------------- #
# Residuals (validation: measured vs predicted)
# --------------------------------------------------------------------------- #
def test_measured_vs_predicted_residual_table():
    res = icp.process([
        {"sample_id": "S1", "element": "Ca", "concentration": 2.1, "unit": "mM",
         "measured_or_predicted": "measured"},
        {"sample_id": "S1", "element": "Ca", "concentration": 2.5, "unit": "mM",
         "measured_or_predicted": "predicted"},
        {"sample_id": "S1", "element": "Si", "concentration": 0.8, "unit": "mM",
         "measured_or_predicted": "measured"},
        {"sample_id": "S1", "element": "Si", "concentration": 0.7, "unit": "mM",
         "measured_or_predicted": "predicted"},
    ])
    by_el = {r.element: r for r in res.residuals}
    assert set(by_el) == {"Ca", "Si"}
    assert by_el["Ca"].residual_mM == pytest.approx(2.1 - 2.5)
    assert by_el["Ca"].percent_difference == pytest.approx(100 * (2.1 - 2.5) / 2.5)
    assert by_el["Si"].residual_mM == pytest.approx(0.8 - 0.7)


def test_no_residuals_when_only_measured_or_only_predicted():
    res = icp.process([{"sample_id": "S1", "element": "Ca", "concentration": 2.1, "unit": "mM",
                        "measured_or_predicted": "measured"}])
    assert res.residuals == []


def test_predicted_zero_gives_undefined_percent_difference():
    res = icp.process([
        {"sample_id": "S1", "element": "Ca", "concentration": 1.0, "unit": "mM",
         "measured_or_predicted": "measured"},
        {"sample_id": "S1", "element": "Ca", "concentration": 0.0, "unit": "mM",
         "measured_or_predicted": "predicted"},
    ])
    assert res.residuals[0].percent_difference is None


# --------------------------------------------------------------------------- #
# Safety: never fabricate measured data; never simulate the plasma
# --------------------------------------------------------------------------- #
def test_refuses_to_synthesize_measured_from_solid_composition():
    assert icp.can_synthesize_measured_from_composition() is False
    assert "fabricat" in icp.SOLID_TO_MEASURED_REFUSAL.lower()
    # There is no public function that maps a solid composition → measured concentrations.
    assert not any("from_solid" in n or "from_composition" in n
                   for n in dir(icp) if callable(getattr(icp, n))
                   and n not in ("can_synthesize_measured_from_composition",))


def test_explanation_states_no_plasma_simulation():
    res = icp.process([{"sample_id": "S", "element": "Ca", "concentration": 1, "unit": "mM"}])
    assert "does not simulate the plasma" in res.explanation


def test_process_never_raises_on_garbage_rows():
    res = icp.process([{}, {"element": None}, {"concentration": "abc", "unit": "mg/L"}, None])
    assert isinstance(res.corrected, list)


_SECRET_RE = re.compile(r"sk-[A-Za-z0-9]{8,}|api[_-]?key|secret", re.I)


def test_no_secret_in_outputs():
    res = icp.process([{"sample_id": "S", "element": "Ca", "concentration": 1, "unit": "mM"}])
    blob = res.explanation + " ".join(res.warnings)
    assert not _SECRET_RE.search(blob)
