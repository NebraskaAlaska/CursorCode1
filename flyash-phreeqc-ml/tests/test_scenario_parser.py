"""Tests for the natural-language simulation planner's parsers.

Covers the rule-based fallback (deterministic extraction + safety warnings), the AI
orchestrator (disabled → fallback, invalid JSON → controlled fallback, valid JSON → AI),
and the scientific safety caveats. No network — the AI client is always a fake.
"""
from __future__ import annotations

import json
import types

import pytest

from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.ai import scenario_parser as sp
from flyash_phreeqc_ml.simulation import rule_parser, scenario_schema as S


# --------------------------------------------------------------------------- #
# Fakes + fixtures
# --------------------------------------------------------------------------- #
class FakeClient:
    """Mimics anthropic.Anthropic(): client.messages.create(**kw) -> scripted text."""

    def __init__(self, text):
        self._text = text
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=self._text)], stop_reason="end")


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    """Default to AI-disabled + no Streamlit secrets, so parse_scenario uses rules unless a
    client is injected."""
    for name in (ai_config.API_KEY_ENV, ai_config.MODEL_ENV, ai_config.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    ai_config.clear_runtime_overrides()
    monkeypatch.setattr(ai_config, "_secrets_get", lambda name: None)
    yield
    ai_config.clear_runtime_overrides()


HCL = ("I have 2 g of Class C fly ash. I add 10 mL of 0.5 M HCl for 60 minutes at room "
       "temperature. I centrifuge, filter the liquid, and measure pH, Ca, Si, Al and Fe. "
       "I want to simulate what should be in the liquid and what may have precipitated.")


def _missing_fields(res):
    return {m.field for m in res.missing}


# --------------------------------------------------------------------------- #
# Rule-based extraction
# --------------------------------------------------------------------------- #
def test_rule_parse_hcl_core_values():
    res = rule_parser.parse(HCL)
    f = res.scenario.to_flat_dict()
    assert res.source == S.SOURCE_RULE
    assert f["material_name"] == "Class C fly ash"
    assert f["solid_mass_g"] == 2.0
    assert f["liquid_volume_mL"] == 10.0
    assert f["leachant_type"] == "HCl"
    assert f["leachant_concentration_M"] == 0.5
    assert f["time_min"] == 60.0
    assert f["liquid_solid_ratio"] == 5.0
    assert f["temperature_C"] == S.ASSUMED_TEMPERATURE_C
    # temperature was assumed (room temperature) → recorded as an assumption.
    assert any(a.field == "temperature_C" for a in res.assumptions)


def test_rule_parse_naoh_and_hours():
    res = rule_parser.parse("Mix 5 g of fly ash with 50 mL of 2 M NaOH for 2 hours.")
    f = res.scenario.to_flat_dict()
    assert f["leachant_type"] == "NaOH"
    assert f["leachant_concentration_M"] == 2.0
    assert f["solid_mass_g"] == 5.0
    assert f["liquid_volume_mL"] == 50.0
    assert f["time_min"] == 120.0          # 2 hours → 120 min
    assert f["liquid_solid_ratio"] == 10.0


def test_rule_parse_water_leaching_needs_no_concentration():
    res = rule_parser.parse("Leach 2 g of fly ash in 20 mL of deionized water for 30 min.")
    f = res.scenario.to_flat_dict()
    assert f["leachant_type"] == "water"
    assert f["leachant_concentration_M"] is None
    # Water leaching must NOT raise a missing-concentration item.
    assert "leachant_concentration_M" not in _missing_fields(res)


def test_rule_parse_centrifuge_and_filter_steps():
    res = rule_parser.parse(
        "The slurry was centrifuged then filtered through a 0.45 µm membrane filter.")
    f = res.scenario.to_flat_dict()
    assert f["centrifuge_used"] is True
    assert f["filtration_used"] is True
    assert f["filter_size_um"] == 0.45


def test_rule_parse_extracts_target_elements():
    res = rule_parser.parse("We measured pH, Ca, Si, Al and Fe in the leachate.")
    assert res.scenario.outputs.target_elements == ["Ca", "Si", "Al", "Fe"]


def test_rule_parse_full_name_elements():
    res = rule_parser.parse("Calcium, silicon, aluminium and iron were measured.")
    assert set(res.scenario.outputs.target_elements) == {"Ca", "Si", "Al", "Fe"}


# --------------------------------------------------------------------------- #
# Missing-field warnings (deterministic safety analysis)
# --------------------------------------------------------------------------- #
def test_missing_solid_mass_warning():
    res = rule_parser.parse("Add 10 mL of 0.5 M NaOH for 60 min to the fly ash.")
    assert "solid_mass_g" in _missing_fields(res)
    m = next(m for m in res.missing if m.field == "solid_mass_g")
    assert m.severity == S.SEVERITY_ERROR


def test_missing_liquid_volume_warning():
    res = rule_parser.parse("Treat 2 g of fly ash with 0.5 M NaOH for 60 min.")
    assert "liquid_volume_mL" in _missing_fields(res)


def test_missing_leachant_concentration_warning():
    res = rule_parser.parse("Treat 2 g of fly ash with 10 mL of HCl for 60 min.")
    assert res.scenario.leachant.leachant_type == "HCl"
    assert res.scenario.leachant.leachant_concentration_M is None
    assert "leachant_concentration_M" in _missing_fields(res)


# --------------------------------------------------------------------------- #
# Scientific caveats (task 6)
# --------------------------------------------------------------------------- #
def test_unsupported_leachant_template_warning():
    res = rule_parser.parse(HCL)
    assert any("PHREEQC template may not support this leachant" in w
               for w in res.scenario.warnings)


def test_precipitation_caveat_present_when_asked():
    res = rule_parser.parse(HCL)
    assert any(w == S.PRECIPITATION_CAVEAT for w in res.scenario.warnings)


def test_naoh_leachant_has_no_template_warning():
    res = rule_parser.parse("Mix 5 g of fly ash with 50 mL of 2 M NaOH for 60 min.")
    assert not any("template may not support" in w for w in res.scenario.warnings)


# --------------------------------------------------------------------------- #
# AI orchestrator
# --------------------------------------------------------------------------- #
def test_parse_scenario_falls_back_to_rules_when_ai_disabled():
    res = sp.parse_scenario("2 g fly ash + 10 mL 0.5 M HCl for 60 min")
    assert res.source == S.SOURCE_RULE
    assert res.scenario.leachant.leachant_type == "HCl"


def test_parse_with_ai_invalid_json_is_controlled():
    res = sp.parse_with_ai(HCL, client=FakeClient("Sure! here you go: not json at all"))
    assert res.ok is False
    assert res.error and "JSON" in res.error
    assert res.raw_response is not None          # kept for debug, in memory only
    assert res.scenario.leachant.leachant_type is None


def test_parse_scenario_invalid_ai_falls_back_to_rules():
    res = sp.parse_scenario(HCL, client=FakeClient("not json"))
    assert res.source == S.SOURCE_RULE_FALLBACK
    assert res.ok is True                        # usable scenario from the rule fallback
    assert res.error and "JSON" in res.error
    assert res.scenario.leachant.leachant_type == "HCl"   # rules still extracted it
    assert any("rule-based fallback" in w for w in res.scenario.warnings)


def test_parse_with_ai_valid_json():
    payload = {
        "material": {"material_name": "Class C fly ash", "material_type": "class_c_fly_ash",
                     "solid_mass_g": 2},
        "leachant": {"leachant_type": "HCl", "leachant_concentration_M": 0.5,
                     "liquid_volume_mL": 10},
        "process": {"time_min": 60, "temperature_C": 25, "CO2_condition": "OA",
                    "centrifuge_used": True, "filtration_used": True},
        "outputs": {"target_elements": ["Ca", "Si"], "desired_outputs": ["liquid_composition"]},
        "assumptions": [], "warnings": ["a custom model warning"], "confidence": 0.9,
    }
    res = sp.parse_with_ai(HCL, client=FakeClient(json.dumps(payload)))
    assert res.ok is True and res.source == S.SOURCE_AI
    f = res.scenario.to_flat_dict()
    assert f["solid_mass_g"] == 2.0 and f["leachant_concentration_M"] == 0.5
    assert f["liquid_solid_ratio"] == 5.0 and f["CO2_condition"] == "OA"
    assert res.confidence == pytest.approx(0.9)
    # AI warnings merge with the code-computed scientific caveats (never replace them).
    assert "a custom model warning" in res.scenario.warnings
    assert any("PHREEQC template may not support this leachant" in w for w in res.scenario.warnings)


def test_parse_with_ai_clamps_unknown_co2_to_none():
    payload = {"material": {"solid_mass_g": 2}, "leachant": {"leachant_type": "NaOH",
               "leachant_concentration_M": 1, "liquid_volume_mL": 10},
               "process": {"CO2_condition": "sealed"}, "outputs": {}, "confidence": 0.5}
    res = sp.parse_with_ai("x", client=FakeClient(json.dumps(payload)))
    assert res.scenario.process.CO2_condition is None     # "sealed" is not allowed → None


def test_injected_client_used_even_without_key():
    # A fake client is honoured regardless of key state (matches the other AI features).
    payload = {"material": {"solid_mass_g": 1}, "leachant": {}, "process": {},
               "outputs": {}, "confidence": 0.3}
    res = sp.parse_scenario("1 g fly ash", client=FakeClient(json.dumps(payload)))
    assert res.source == S.SOURCE_AI and res.ok is True
