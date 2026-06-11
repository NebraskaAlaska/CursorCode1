"""Tests for the optional AI import-assist layer.

The Anthropic client is always mocked — **no network** is touched. Coverage:

* JSON-parse-failure fallback (bad model output → empty result, no raise);
* rule-first short-circuit (fully parseable ids never reach the mock client);
* provenance tagging (source keyword → metadata_provenance value);
* disabled-mode behaviour when ``ANTHROPIC_API_KEY`` is absent.
"""
from __future__ import annotations

import types

import pytest

from flyash_phreeqc_ml import profiles
from flyash_phreeqc_ml.ai import import_assist as ia


# --------------------------------------------------------------------------- #
# A minimal fake Anthropic client (mimics client.messages.create -> response).
# --------------------------------------------------------------------------- #
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class FakeClient:
    """Records calls and returns a canned text body (or raises)."""

    def __init__(self, text="", raise_exc=None):
        self._text = text
        self._raise = raise_exc
        self.calls = 0
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self._raise is not None:
            raise self._raise
        return _Resp(self._text)


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """Default every test to the disabled state; tests that need a client inject one."""
    monkeypatch.delenv(ia.API_KEY_ENV, raising=False)


# --------------------------------------------------------------------------- #
# JSON-parse-failure fallback
# --------------------------------------------------------------------------- #
def test_column_mapping_bad_json_falls_back_to_empty():
    fake = FakeClient(text="Sure! here is the mapping: not-json at all ```")
    out = ia.propose_column_mapping(
        ["pH", "Calcium"], [["13.1", "5.2"]],
        ia.default_target_schema(), client=fake,
    )
    assert out == []           # bad JSON → empty, no exception
    assert fake.calls == 1     # it did call the model


def test_classify_sheets_bad_json_falls_back():
    fake = FakeClient(text="```json\n{ this is broken,,, ```")
    out = ia.classify_sheets([{"sheet": "S1", "headers": ["pH"], "rows": [["13"]]}], client=fake)
    assert out == []


def test_request_json_handles_api_error():
    fake = FakeClient(raise_exc=RuntimeError("api down"))
    out = ia.propose_column_mapping(["pH"], [["13"]], ["final_pH"], client=fake)
    assert out == []           # API error degrades gracefully


def test_column_mapping_strips_code_fences_and_filters():
    body = (
        "```json\n"
        '[{"source_col": "pH", "target_col": "final_pH", "unit_guess": null, '
        '"confidence": 0.9, "evidence": "header says pH"},'
        ' {"source_col": "Calcium", "target_col": "Ca_mM", "unit_guess": "mg/L", '
        '"confidence": 1.5, "evidence": "element"},'
        ' {"source_col": "NOT_A_HEADER", "target_col": "Si_mM", "confidence": 0.5},'
        ' {"source_col": "pH", "target_col": "bogus_target", "confidence": 0.3}]\n'
        "```"
    )
    fake = FakeClient(text=body)
    out = ia.propose_column_mapping(
        ["pH", "Calcium"], [["13.1", "5.2"]],
        ["final_pH", "Ca_mM", "Si_mM"], client=fake,
    )
    by_src = {r["source_col"]: r for r in out}
    assert "NOT_A_HEADER" not in by_src           # unknown header dropped
    assert by_src["pH"]["target_col"] in {"final_pH", None}
    assert by_src["Calcium"]["target_col"] == "Ca_mM"
    assert by_src["Calcium"]["unit_guess"] == "mg/L"
    assert by_src["Calcium"]["confidence"] == 1.0  # clamped to [0,1]


# --------------------------------------------------------------------------- #
# Rule-first short-circuit
# --------------------------------------------------------------------------- #
def test_parse_sample_names_rule_first_never_calls_model():
    # Canonical fly-ash ids parse fully by rule → the client must not be touched.
    ids = ["CFA-NaOH0.5M-LS5-10min-OA-R1", "CFA-NaOH0.5M-LS5-10min-OA-R2"]
    fake = FakeClient(text='[]')  # would be used only if an id were unparsed
    out = ia.parse_sample_names(ids, profiles.FLY_ASH_PROFILE, client=fake)
    assert fake.calls == 0
    assert [r["source"] for r in out] == [ia.SOURCE_RULE, ia.SOURCE_RULE]
    first = out[0]["fields"]
    assert first["leachant"].upper() == "NAOH"
    assert first["concentration"] == "0.5"
    assert first["condition_code"] == "OA"
    assert first["time_min"] == "10"
    assert first["replicate"] == "R1"


def test_parse_sample_names_sends_only_unparsed_to_model():
    ids = ["CFA-NaOH0.5M-LS5-10min-OA-R1", "weird-sample-XYZ"]
    body = ('[{"sample_id": "weird-sample-XYZ", "fields": {"leachant": "NaOH"}, '
            '"confidence": 0.4, "note": "guessed"}]')
    fake = FakeClient(text=body)
    out = ia.parse_sample_names(ids, profiles.FLY_ASH_PROFILE, client=fake)
    assert fake.calls == 1                              # only the leftover triggered a call
    # The unparsed id was the only one sent.
    sent = fake.last_kwargs["messages"][0]["content"]
    assert "weird-sample-XYZ" in sent
    assert "CFA-NaOH0.5M-LS5-10min-OA-R1" not in sent
    by_id = {r["sample_id"]: r for r in out}
    assert by_id["CFA-NaOH0.5M-LS5-10min-OA-R1"]["source"] == ia.SOURCE_RULE
    assert by_id["weird-sample-XYZ"]["source"] == ia.SOURCE_AI


def test_parse_sample_names_ai_bad_json_yields_placeholder():
    ids = ["totally-opaque-name"]
    fake = FakeClient(text="nope, not json")
    out = ia.parse_sample_names(ids, profiles.FLY_ASH_PROFILE, client=fake)
    assert out[0]["source"] is None
    assert out[0]["fields"] == {}


# --------------------------------------------------------------------------- #
# Provenance tagging
# --------------------------------------------------------------------------- #
def test_provenance_mapping():
    assert ia.provenance_value(ia.SOURCE_RULE) == "rule"
    assert ia.provenance_value(ia.SOURCE_AI) == "ai-confirmed"
    assert ia.provenance_value(ia.SOURCE_MANUAL) == "manual"
    assert ia.provenance_value(None) == "manual"
    assert ia.build_provenance_column([ia.SOURCE_RULE, ia.SOURCE_AI, None]) == [
        "rule", "ai-confirmed", "manual",
    ]


def test_provenance_from_rule_parsed_names():
    ids = ["CFA-NaOH0.5M-LS5-10min-OA-R1"]
    out = ia.parse_sample_names(ids, profiles.FLY_ASH_PROFILE, client=FakeClient())
    sources = [r["source"] for r in out]
    assert ia.build_provenance_column(sources) == ["rule"]


# --------------------------------------------------------------------------- #
# Disabled-mode (no API key)
# --------------------------------------------------------------------------- #
def test_is_enabled_false_without_key(monkeypatch):
    monkeypatch.delenv(ia.API_KEY_ENV, raising=False)
    assert ia.is_enabled() is False


def test_disabled_mode_returns_empty_without_client(monkeypatch):
    monkeypatch.delenv(ia.API_KEY_ENV, raising=False)
    assert ia.classify_sheets([{"sheet": "S", "headers": ["pH"], "rows": [["13"]]}]) == []
    assert ia.propose_column_mapping(["pH"], [["13"]], ["final_pH"]) == []


def test_disabled_mode_parse_names_still_rule_parses(monkeypatch):
    monkeypatch.delenv(ia.API_KEY_ENV, raising=False)
    ids = ["CFA-NaOH0.5M-LS5-10min-OA-R1", "opaque"]
    # No client passed and no key → AI path is skipped entirely; rules still run.
    out = ia.parse_sample_names(ids)
    by_id = {r["sample_id"]: r for r in out}
    assert by_id["CFA-NaOH0.5M-LS5-10min-OA-R1"]["source"] == ia.SOURCE_RULE
    assert by_id["opaque"]["source"] is None
