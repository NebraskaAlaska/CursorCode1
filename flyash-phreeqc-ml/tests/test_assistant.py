"""Tests for the grounded "Ask the assistant" layer (ai/assistant.py).

Pins (per the spec): the read-only tool wrappers return correct shapes, the
dispatcher round-trip (tool_use → tool_result → final text) works with a mocked
client, and the assistant disables cleanly without an API key. Synthetic data only;
no network — the Anthropic client is always a fake.
"""
from __future__ import annotations

import json
import types

import pandas as pd
import pytest

from flyash_phreeqc_ml import replicates
from flyash_phreeqc_ml.ai import assistant as A


# --------------------------------------------------------------------------- #
# Synthetic run context
# --------------------------------------------------------------------------- #
def _ctx():
    data = pd.DataFrame([
        {"sample_id": f"X{i}", "leachant": "NaOH", "liquid_solid_ratio": 5,
         "CO2_condition": "OA", "final_pH": 13.0 + 0.01 * i, "Ca_mM": 1.0 + 0.1 * i}
        for i in range(4)
    ])
    manifest = pd.DataFrame([
        {"phreeqc_record_key": "k1", "state": "batch", "liquid_solid_ratio": 5.0,
         "CO2_condition": "OA", "temperature_C": float("nan"), "scenario_label": "k1",
         "predicted_pH": 13.1, "predicted_Ca_mM": 0.8},
    ])
    sample_mapping = pd.DataFrame([{"sample_id": f"X{i}", "phreeqc_record_key": "k1"}
                                  for i in range(4)])
    condition_mapping = pd.DataFrame(
        columns=["condition_key", "phreeqc_record_key", "notes", "override"])
    comp = data.copy()
    comp["phreeqc_record_key"] = "k1"
    comp["phreeqc_pH"] = 13.1
    comp["residual_pH"] = comp["final_pH"] - comp["phreeqc_pH"]
    comp["phreeqc_Ca_mM"] = 0.8
    comp["residual_Ca"] = comp["Ca_mM"] - comp["phreeqc_Ca_mM"]
    return A.RunContext(run_name="synthetic_test_run", data=data,
                        sample_mapping=sample_mapping, condition_mapping=condition_mapping,
                        manifest=manifest, comparison_df=comp)


# --------------------------------------------------------------------------- #
# Fake Anthropic client (records calls; scripted responses)
# --------------------------------------------------------------------------- #
def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _tool_use_block(tid, name, tool_input):
    return types.SimpleNamespace(type="tool_use", id=tid, name=name, input=tool_input)


def _resp(content):
    return types.SimpleNamespace(content=content, stop_reason="x")


class FakeClient:
    """Mimics anthropic.Anthropic(): ``client.messages.create(**kw)`` returns scripted."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._scripted.pop(0)


# --------------------------------------------------------------------------- #
# Tool wrapper shapes
# --------------------------------------------------------------------------- #
def test_get_mapping_overview_shape():
    out = A.get_mapping_overview(_ctx())
    assert out["source"] == "get_mapping_overview"
    for key in ("status_counts", "n_mapped", "all_exact", "overall_status",
                "n_conditions", "conditions"):
        assert key in out
    assert isinstance(out["conditions"], list)


def test_get_inclusion_counts_shape_and_validity_flag():
    out = A.get_inclusion_counts(_ctx())
    assert out["source"] == "get_inclusion_counts"
    assert isinstance(out["variables"], list) and out["variables"]
    v0 = out["variables"][0]
    for key in ("variable", "rows_plotted", "rows_excluded", "validity", "validity_message"):
        assert key in v0
    assert isinstance(out["any_variable_valid"], bool)


def test_get_unsafe_mappings_shape():
    out = A.get_unsafe_mappings(_ctx())
    assert out["source"] == "get_unsafe_mappings"
    assert "n_unsafe" in out and isinstance(out["unsafe"], list)


def test_get_needed_simulations_shape():
    out = A.get_needed_simulations(_ctx())
    assert out["source"] == "get_needed_simulations"
    assert "n_needed" in out and isinstance(out["conditions"], list)


def test_get_bias_table_shape_carries_non_claim():
    out = A.get_bias_table(_ctx(), min_n=5)
    assert out["source"] == "get_bias_table"
    assert out["non_claim"]                       # the explicit non-claim line is present
    assert "rows" in out and isinstance(out["rows"], list)
    assert "n_sufficient" in out


def test_get_comparison_rows_caps_and_counts():
    out = A.get_comparison_rows(_ctx(), limit=2)
    assert out["source"] == "get_comparison_rows"
    assert out["n_total"] == 4
    assert out["n_returned"] == 2
    assert len(out["rows"]) == 2
    # The hard cap is honoured even if a huge limit is requested.
    out2 = A.get_comparison_rows(_ctx(), limit=10_000)
    assert len(out2["rows"]) == 4


def test_get_run_provenance_handles_missing_run():
    # No run on disk → graceful dict (not an exception), with the documented keys.
    out = A.get_run_provenance(_ctx())
    assert out["source"] == "get_run_provenance"
    assert "is_current" in out and "stale_reasons" in out
    assert out["is_current"] is False


def test_get_mapping_trace_requires_condition_key():
    err = A.get_mapping_trace(_ctx(), "")
    assert "error" in err
    table = A.get_mapping_overview(_ctx())
    ck = table["conditions"][0]["condition_key"]
    tr = A.get_mapping_trace(_ctx(), ck)
    assert tr["condition_key"] == ck
    assert isinstance(tr["candidates"], list)


# --------------------------------------------------------------------------- #
# Dispatcher + JSON-ability
# --------------------------------------------------------------------------- #
def test_dispatch_unknown_tool_returns_error():
    out = A.dispatch(_ctx(), "no_such_tool", {})
    assert "error" in out


def test_dispatch_results_are_json_serialisable():
    for name in ("get_mapping_overview", "get_inclusion_counts", "get_bias_table",
                 "get_comparison_rows", "get_needed_simulations", "get_unsafe_mappings"):
        out = A.dispatch(_ctx(), name, {})
        json.dumps(out)  # must not raise (no numpy/NaN leaking through)


# --------------------------------------------------------------------------- #
# Dispatcher round-trip with a mocked client
# --------------------------------------------------------------------------- #
def test_round_trip_tool_use_then_final_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # so a real client could exist
    scripted = [
        _resp([_tool_use_block("t1", "get_mapping_overview", {})]),
        _resp([_text_block("Per get_mapping_overview, the run has conditions mapped.")]),
    ]
    client = FakeClient(scripted)
    ans = A.answer(_ctx(), "How is the mapping looking?", client=client)

    assert ans.ok is True
    assert "get_mapping_overview" in ans.text
    assert len(ans.trace) == 1
    assert ans.trace[0]["tool"] == "get_mapping_overview"
    assert "summary" in ans.trace[0]
    # Two model calls: the tool_use turn, then the final answer.
    assert len(client.calls) == 2
    # The second call carried a tool_result back to the model.
    second_msgs = client.calls[1]["messages"]
    last = second_msgs[-1]
    assert last["role"] == "user"
    assert any(isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"])


def test_multiple_tool_uses_in_one_turn(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    scripted = [
        _resp([_tool_use_block("t1", "get_inclusion_counts", {}),
               _tool_use_block("t2", "get_bias_table", {"min_n": 5})]),
        _resp([_text_block("Grounded answer.")]),
    ]
    ans = A.answer(_ctx(), "Summarise validity and bias.", client=FakeClient(scripted))
    assert ans.ok is True
    assert {t["tool"] for t in ans.trace} == {"get_inclusion_counts", "get_bias_table"}


def test_history_is_passed_through_session_only(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = FakeClient([_resp([_text_block("ok")])])
    history = [{"role": "user", "content": "earlier question"},
               {"role": "assistant", "content": "earlier answer"}]
    A.answer(_ctx(), "follow up", client=client, history=history)
    msgs = client.calls[0]["messages"]
    # History precedes the new question, in order; nothing is persisted anywhere.
    assert msgs[0] == {"role": "user", "content": "earlier question"}
    assert msgs[1] == {"role": "assistant", "content": "earlier answer"}
    assert msgs[-1] == {"role": "user", "content": "follow up"}


# --------------------------------------------------------------------------- #
# Disabled without an API key
# --------------------------------------------------------------------------- #
def test_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ans = A.answer(_ctx(), "anything?", client=None)
    assert ans.ok is False
    assert ans.text == ""
    assert "disabled" in (ans.error or "").lower()
    assert ans.trace == []


def test_is_enabled_false_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert A.is_enabled() is False
