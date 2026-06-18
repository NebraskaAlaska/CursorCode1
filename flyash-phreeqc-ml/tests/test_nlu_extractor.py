"""Tests for the agent's **natural-language understanding** layer (mocked AI; no network).

Covers the robustness contract: messy / informal / typo-filled prompts still yield the right
structured set-up — deterministically when AI is off, and via a *validated* AI ``understanding``
block when AI is on — while the safety boundary holds (no composition / release fraction / result
/ validation is ever extracted, impossible values are rejected, assumed values are flagged).
"""
from __future__ import annotations

import json
import types

import pytest

from flyash_phreeqc_ml.agent import agent_state, domains
from flyash_phreeqc_ml.agent import nlu_extractor as nx
from flyash_phreeqc_ml.ai import config as ai_config


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class FakeClient:
    """anthropic.Anthropic() stand-in returning scripted JSON text."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.messages = self

    def create(self, **kwargs):
        payload = (self._payloads.pop(0) if self._payloads
                   else {"assistant_message": "ok", "action": {"action_name": "ASK_USER"}})
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end")


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    for name in (ai_config.API_KEY_ENV, ai_config.MODEL_ENV, ai_config.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    ai_config.clear_runtime_overrides()
    monkeypatch.setattr(ai_config, "_secrets_get", lambda name: None)
    yield
    ai_config.clear_runtime_overrides()


def _extract(message, state=None):
    return nx.extract(message, state=state or agent_state.AgentState())


# --------------------------------------------------------------------------- #
# Text normalization
# --------------------------------------------------------------------------- #
def test_normalize_units_and_spellings():
    cases = {
        "naoh": "NaOH", "na oh": "NaOH", "sodum hydroxde": "NaOH",
        "sodium hydroxide": "NaOH", "hcl": "HCl", "hydrochloric acid": "HCl",
        "flyash": "fly ash", "fli ash": "fly ash", "CFA": "Class C fly ash",
        "redmud": "red mud", "bauxite residue": "red mud",
        ".5M": "0.5 M", "0.5m": "0.5 M",
    }
    for raw, expect in cases.items():
        out, _notes = nx.normalize_text(raw)
        assert expect in out, f"{raw!r} → {out!r} (wanted {expect!r})"
    # 10 mL must NOT be turned into molarity, and clean text is unchanged.
    assert "0.5 M" in nx.normalize_text("0.5 M NaOH")[0]
    assert nx.normalize_text("10 mL")[0] == "10 mL"


def test_canonical_leachant_is_careful_about_acid():
    assert nx.canonical_leachant("naoh")[0] == "NaOH"
    assert nx.canonical_leachant("hydrochloric")[0] == "HCl"
    assert nx.canonical_leachant("water")[0] == "water"
    # A generic "acid leach" with no named reagent is AMBIGUOUS — never silently HCl.
    canon, ambiguous = nx.canonical_leachant("acid leach")
    assert canon is None and ambiguous is True


# --------------------------------------------------------------------------- #
# Numeric validation / rejection
# --------------------------------------------------------------------------- #
def test_validate_rejects_impossible_values():
    assert nx.validate_value("solid_mass_g", -2)[1] is not None          # negative mass rejected
    assert nx.validate_value("time_min", -5)[1] is not None              # negative time rejected
    assert nx.validate_value("leachant_concentration_M", 99)[1] is not None  # absurd molarity
    assert nx.validate_value("temperature_C", 5000)[1] is not None       # impossible temperature
    # A valid value passes; a high-but-possible temperature passes with a note.
    assert nx.validate_value("solid_mass_g", 2.0) == (2.0, None, None)
    val, reject, note = nx.validate_value("temperature_C", 200)
    assert reject is None and note is not None


def test_deterministic_extract_rejects_negative_quantities():
    r = _extract("use -2 g of fly ash in 10 mL for -5 min")
    assert "solid_mass_g" not in r.delta and "time_min" not in r.delta
    assert r.delta.get("liquid_volume_mL") == 10.0
    rejected_fields = {x["field"] for x in r.rejected}
    assert {"solid_mass_g", "time_min"} <= rejected_fields


# --------------------------------------------------------------------------- #
# Messy prompts, deterministic (AI off)
# --------------------------------------------------------------------------- #
def test_messy_leaching_prompt_A():
    r = _extract("im leeching class c fli ash w naoh .5m 2g 10ml for 1hr room temp wanna ph ca si")
    assert nx.classify_message(
        "im leeching class c fli ash w naoh .5m 2g 10ml for 1hr room temp wanna ph ca si"
    ) == domains.LEACHING_GEOCHEMISTRY
    assert r.delta["material_name"] == "Class C fly ash"
    assert r.delta["leachant_type"] == "NaOH"
    assert r.delta["leachant_concentration_M"] == 0.5
    assert r.delta["solid_mass_g"] == 2.0
    assert r.delta["liquid_volume_mL"] == 10.0
    assert r.delta["time_min"] == 60.0
    assert r.delta["temperature_C"] == 25.0                  # room temp → 25 (assumed)
    assert {"Ca", "Si"} <= set(r.delta["target_elements"])
    assert "pH" in r.delta["desired_outputs"]
    # Temperature is flagged as an assumption to confirm (never silently trusted).
    assert any(spec[0] == "temperature_C" for spec in r.assumption_specs)
    assert r.source == nx.SRC_RULE and r.limited_without_ai is True


def test_messy_plastic_prompt_B_is_planning_only():
    msg = "mixing fli ash w waste plstic need compresive strengh 28 days"
    assert nx.classify_message(msg) in (domains.POLYMER_COMPOSITE, domains.MECHANICAL_TESTING)
    r = _extract(msg)
    # No leaching set-up is invented for a strength test (no leachant / mass / time).
    assert "leachant_type" not in r.delta
    assert "solid_mass_g" not in r.delta and "time_min" not in r.delta


def test_messy_red_mud_prompt_C():
    msg = "redmud hcl leach 1g 20ml .1m 2hr estimate fe al na ph maybe sc"
    assert nx.classify_message(msg) == domains.LEACHING_GEOCHEMISTRY
    r = _extract(msg)
    assert r.delta["material_name"] == "red mud"
    assert r.delta["leachant_type"] == "HCl"
    assert r.delta["leachant_concentration_M"] == 0.1
    assert r.delta["solid_mass_g"] == 1.0
    assert r.delta["liquid_volume_mL"] == 20.0
    assert r.delta["time_min"] == 120.0
    assert {"Fe", "Al", "Na", "Sc"} <= set(r.delta["target_elements"])


# --------------------------------------------------------------------------- #
# AI understanding → validated delta (schema repair + safety)
# --------------------------------------------------------------------------- #
def test_repair_understanding_validates_and_strips_forbidden():
    cur = agent_state.AgentState().scenario.to_flat_dict()
    understanding = {
        "material_name": "red mud", "leachant_type": "sodium hydroxide",
        "leachant_concentration_M": 0.5, "solid_mass_g": 2, "liquid_volume_mL": 10,
        "time_min": 60, "temperature_C": None,
        "target_elements": ["fe", "al"], "desired_outputs": ["pH"],
        "CO2_condition": "sealed",                          # invalid → dropped
        "ambiguous_fields": ["temperature_C"],
        "assumptions": [{"field": "temperature_C", "assumed_value": 25,
                         "reason": "room temp assumed", "needs_confirmation": True}],
        # Forbidden — must never be extracted:
        "composition": {"CaO": 24}, "release_fraction": 0.1, "pH": 13.2,
        "validation_status": "validated",
        "confidence": 0.9, "domain_hint": "leaching_geochemistry",
    }
    res = nx.repair_understanding(understanding, current_flat=cur)
    assert res.delta["leachant_type"] == "NaOH"             # canonicalized
    assert res.delta["target_elements"] == ["Fe", "Al"]     # normalized symbols
    assert res.delta["temperature_C"] == 25.0               # assumed value folded in…
    assert any(s[0] == "temperature_C" for s in res.assumption_specs)  # …and flagged
    assert "temperature_C" in res.ambiguous_fields
    # No forbidden field survives.
    for forbidden in ("composition", "release_fraction", "pH", "validation_status", "CO2_condition"):
        assert forbidden not in res.delta
    assert res.confidence == 0.9 and res.domain_hint == domains.LEACHING_GEOCHEMISTRY


def test_repair_rejects_impossible_ai_values():
    cur = agent_state.AgentState().scenario.to_flat_dict()
    res = nx.repair_understanding(
        {"solid_mass_g": -3, "temperature_C": 9000, "leachant_concentration_M": 0.5},
        current_flat=cur)
    assert "solid_mass_g" not in res.delta and "temperature_C" not in res.delta
    assert res.delta.get("leachant_concentration_M") == 0.5
    assert {x["field"] for x in res.rejected} == {"solid_mass_g", "temperature_C"}


def test_extract_uses_ai_understanding_when_present():
    payload = {"assistant_message": "Got it.",
               "understanding": {"material_name": "Class C fly ash", "leachant_type": "NaOH",
                                 "leachant_concentration_M": 0.5, "solid_mass_g": 2,
                                 "liquid_volume_mL": 10, "time_min": 60,
                                 "target_elements": ["Ca"], "desired_outputs": ["pH"],
                                 "domain_hint": "leaching_geochemistry"},
               "action": {"action_name": "ASK_USER"}, "confidence": 0.95}
    r = nx.extract("messy text", state=agent_state.AgentState(), client=FakeClient([payload]))
    assert r.used_ai is True and r.source == nx.SRC_AI
    assert r.delta["solid_mass_g"] == 2.0 and r.delta["leachant_type"] == "NaOH"
    assert r.action is not None and r.action.action_name == "ASK_USER"
    assert r.confidence == 0.95                             # top-level model confidence captured


def test_invalid_ai_json_falls_back_safely():
    # The model returns non-JSON garbage → extract must NOT crash; it falls back to the rules.
    r = nx.extract("leach 2 g fly ash in 10 mL 0.5 M NaOH",
                   state=agent_state.AgentState(), client=FakeClient(["not json at all <<>>"]))
    assert r.source == nx.SRC_RULE_FALLBACK
    assert r.limited_without_ai is True and r.ai_error
    assert r.delta.get("solid_mass_g") == 2.0               # rules still extracted the fields


def test_ai_action_without_understanding_still_parses_fields():
    # An action but no `understanding` block → fields come from the rule parse, action from AI.
    payload = {"assistant_message": "ok", "action": {"action_name": "BUILD_PHREEQC_PREVIEW"}}
    r = nx.extract("leach 2 g fly ash in 10 mL 0.5 M NaOH",
                   state=agent_state.AgentState(), client=FakeClient([payload]))
    assert r.used_ai is True
    assert r.delta.get("solid_mass_g") == 2.0
    assert r.action.action_name == "BUILD_PHREEQC_PREVIEW"


# --------------------------------------------------------------------------- #
# Conflict detection + understanding card
# --------------------------------------------------------------------------- #
def test_compute_changes_detects_corrections():
    current = {"temperature_C": 25.0, "target_elements": ["Ca", "Si"]}
    changes = nx.compute_changes({"temperature_C": 40.0}, current)
    assert changes and changes[0]["field"] == "temperature_C"
    assert changes[0]["old"] == 25.0 and changes[0]["new"] == 40.0
    # A first-time fill is not a "change".
    assert nx.compute_changes({"solid_mass_g": 2.0}, current) == []
    # Dropping a previously-stated element is a change; a pure addition is not.
    assert nx.compute_changes({"target_elements": ["Ca"]}, current)        # Si dropped
    assert nx.compute_changes({"target_elements": ["Ca", "Si", "Al"]}, current) == []


def test_thermal_then_leach_separates_temperature():
    """A calcination temperature is captured as a pretreatment temp, NOT the leach temperature_C."""
    r = _extract("thermal treat bauxite residue in air 900 C then leach it")
    assert r.delta.get("temperature_C") is None                 # 900 is NOT the leach temp
    assert r.pretreatment_temperature_C == 900.0
    assert "temperature_C" in r.ambiguous_fields                 # asks for the leach temperature
    # A genuinely heated leach (below the calcination threshold) keeps its leach temperature.
    r2 = _extract("leach 2 g fly ash in 10 mL NaOH heated to 80 C")
    assert r2.delta.get("temperature_C") == 80.0
    assert r2.pretreatment_temperature_C is None


def test_unsupported_elements_captured_not_dropped():
    r = _extract("battery cathode powder heated and leached for ni co mn")
    assert set(["Ni", "Co", "Mn"]).issubset(set(r.unsupported_elements))
    # The out-of-scope symbols are NOT silently promoted into the supported target_elements set.
    assert not (set(["Ni", "Co", "Mn"]) & set(r.delta.get("target_elements") or []))
    # A stray "co" word is not read as cobalt; a real cluster is.
    assert nx.detect_unsupported_elements("the company co-located the samples") == []
    assert "Cu" in nx.detect_unsupported_elements("recover ni co mn cu from the leachate")


def test_bare_fly_ash_is_not_assumed_class_c():
    """Deterministic extraction keeps a bare 'fly ash' generic (class unknown) — never Class C."""
    r = _extract("leach 2 g fly ash in 10 mL 0.5 M NaOH")
    assert r.delta.get("material_name") == "fly ash"            # not "Class C fly ash"
    # An explicit class is honored.
    assert _extract("leach class c fly ash in NaOH").delta.get("material_name") == "Class C fly ash"


def test_build_understanding_card_shape():
    s = agent_state.AgentState()
    r = nx.extract("leach 2 g fly ash in 10 mL 0.5 M NaOH at 25 C, want Ca and pH", state=s)
    s.apply_delta(r.delta, assumption_specs=r.assumption_specs)
    s.domain = nx.classify_message(s.experiment_text or "leach fly ash naoh")
    card = nx.build_understanding_card(s, r)
    for key in ("domain", "domain_label", "executable", "material", "leachant",
                "key_variables", "target_elements", "missing", "assumptions",
                "confidence", "source", "source_label"):
        assert key in card
    assert card["material"] == "fly ash"
    assert card["leachant"].startswith("NaOH")
    # The card is a plain JSON-safe dict (no raw model text, no objects).
    json.dumps(card)
