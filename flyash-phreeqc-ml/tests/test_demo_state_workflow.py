"""Post-demo stabilization pins for the Assistant → PHREEQC workflow + live-AI status.

These lock the demo-exposed bugs so they can't regress:

* live-AI status is one shared source of truth (Settings == Assistant);
* a one-turn AI failure falls back without permanently disabling AI;
* PHREEQC availability is reported the same everywhere, with a clear local-vs-container reason;
* a confirmed material profile and a chosen release model both persist (stable identity) and a
  display-name change never invalidates the profile;
* changing the release model invalidates the stale preview but keeps the confirmed composition;
* a ready preview is stored as the executable pending run, and confirming runs that stored
  snapshot rather than rebuilding;
* missing PHREEQC gives a clear, non-crashing unavailable state;
* a completed result is visible in both the Assistant and the Results section;
* "0.5 M" is never parsed/shown as "5 M";
* no API key ever appears in messages, provenance, or committed source.

No real AI call or PHREEQC run happens (the client / executor are faked or unconfigured).
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.agent import agent_actions as A
from flyash_phreeqc_ml.agent import agent_orchestrator as orch
from flyash_phreeqc_ml.agent import agent_state, domains
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.materials import profile_schema as mp
from flyash_phreeqc_ml.simulation import phreeqc_executor, source_terms

FAKE_KEY = "sk-ant-TESTKEY-not-real-do-not-leak-demo-0001"


# --------------------------------------------------------------------------- #
# Fakes + fixtures (mirroring tests/test_agent.py)
# --------------------------------------------------------------------------- #
class FakeClient:
    """anthropic.Anthropic() stand-in returning scripted JSON for one action."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.messages = self

    def create(self, **kwargs):
        payload = (self._payloads.pop(0) if self._payloads
                   else {"assistant_message": "ok", "action": {"action_name": "ASK_USER"}})
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end")


def _action(name, **arguments):
    return {"assistant_message": f"doing {name}", "reasoning_summary": "ok", "confidence": 0.8,
            "action": {"action_name": name, "arguments": arguments}}


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    """AI disabled + no Streamlit secrets, so AI runs only when a fake client is injected."""
    for name in (ai_config.API_KEY_ENV, ai_config.MODEL_ENV, ai_config.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    ai_config.clear_runtime_overrides()
    monkeypatch.setattr(ai_config, "_secrets_get", lambda name: None)
    yield
    ai_config.clear_runtime_overrides()


@pytest.fixture
def usable_profile():
    """A user-confirmed Class C fly ash composition (usable for the input preview)."""
    return mp.MaterialProfile(
        profile_id="fa-demo", material_name="Synthetic demo Class C fly ash",
        material_type="class_c_fly_ash", composition_basis=mp.BASIS_OXIDE_WT,
        entries=[mp.CompositionEntry("CaO", 24.0), mp.CompositionEntry("SiO2", 34.0),
                 mp.CompositionEntry("Al2O3", 18.0), mp.CompositionEntry("Na2O", 2.0),
                 mp.CompositionEntry("K2O", 1.0)],
        verification_status=mp.STATUS_USER_CONFIRMED)


@pytest.fixture
def release_model():
    return source_terms.global_release(0.01)


def _ready_state(profile, release):
    """A complete NaOH leaching scenario + usable profile + release model (the demo prompt)."""
    s = agent_state.AgentState()
    orch.respond(s, "im leeching class c fli ash w naoh .5m 2g 10ml for 1hr room temp wanna ph ca si",
                 material_profile=profile, release_model=release)
    return s


def _ok_execution(pH=13.48):
    def fake_run_and_parse(preview, **kw):
        return (phreeqc_executor.ExecutionResult(
                    "ASSISTANT-001", phreeqc_executor.STATUS_SUCCESS, output_path="x.pqo"),
                phreeqc_executor.ParsedSimulation(
                    "ASSISTANT-001", phreeqc_executor.PARSE_PARSED, pH=pH,
                    element_totals_mM={"Ca": 5.0, "Si": 2.0, "Al": 0.3},
                    saturation_indices=[{"phase": "Calcite", "SI": 0.12}]))
    return fake_run_and_parse


# --------------------------------------------------------------------------- #
# 1) Settings live-AI enabled → the Assistant receives use_ai=True (one source)
# --------------------------------------------------------------------------- #
def test_live_ai_status_is_single_source_of_truth(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, FAKE_KEY)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    cfg = ai_config.resolve_config()

    on = ai_config.live_ai_status(cfg, True)            # toggle on + capable → active
    assert on.active and on.capable and on.toggle_on
    assert "Live AI is on" in on.reason
    assert FAKE_KEY not in on.reason and FAKE_KEY not in json.dumps(on.to_safe_dict())

    off = ai_config.live_ai_status(cfg, False)          # capable but toggle off → not active
    assert not off.active and off.capable
    # back-compat boolean matches the status.active exactly
    assert ai_config.live_ai_active(cfg, True) is True
    assert ai_config.live_ai_active(cfg, False) is False

    # No key → never active even with a stale toggle on (capability wins).
    monkeypatch.delenv(ai_config.API_KEY_ENV, raising=False)
    cfg2 = ai_config.resolve_config()
    assert ai_config.live_ai_status(cfg2, True).active is False


# --------------------------------------------------------------------------- #
# 2) A failed AI call falls back for ONE turn without permanently disabling AI
# --------------------------------------------------------------------------- #
def test_failed_ai_call_falls_back_one_turn(monkeypatch):
    # AI is capable (key + SDK present) and requested, but every client call fails.
    monkeypatch.setenv(ai_config.API_KEY_ENV, FAKE_KEY)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    from flyash_phreeqc_ml.ai import client as ai_client
    monkeypatch.setattr(ai_client, "get_client",
                        lambda *a, **k: types.SimpleNamespace(ok=False, client=None,
                                                              error_code="sim", message="sim fail"))
    s = agent_state.AgentState()
    # use_ai=True but the client fails → deterministic fallback for this turn, AI not disabled.
    r = orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min", use_ai=True)
    assert r.used_ai is False
    assert s.last_ai_fell_back is True
    assert orch.AI_FALLBACK_NOTE in r.assistant_message       # surfaced, never silent
    assert r.assistant_message                                # still a helpful reply (no crash)
    # The key never appears in the surfaced fallback message.
    assert FAKE_KEY not in r.assistant_message


# --------------------------------------------------------------------------- #
# 3) PHREEQC configured → Settings AND the Assistant both report ready (same fn)
# --------------------------------------------------------------------------- #
def test_phreeqc_env_makes_both_report_ready(monkeypatch, tmp_path):
    exe = tmp_path / "phreeqc"
    exe.write_text("#!/bin/sh\n")
    db = tmp_path / "phreeqc.dat"
    db.write_text("# fake database")
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", str(exe))
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", str(db))

    av = phreeqc_executor.check_availability()
    assert av.can_run is True
    hint = phreeqc_executor.availability_hint(av)
    assert "configured and ready" in hint
    # Both Settings (_render_phreeqc_engine) and the Assistant (_render_context_providers) call the
    # SAME phreeqc_executor.check_availability()/availability_hint — proven at the UI level below.


def test_phreeqc_env_ready_in_settings_and_assistant_ui(monkeypatch, tmp_path):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    exe = tmp_path / "phreeqc"
    exe.write_text("#!/bin/sh\n")
    db = tmp_path / "phreeqc.dat"
    db.write_text("# fake database")
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", str(exe))
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", str(db))

    at = AppTest.from_file("app.py", default_timeout=120).run()
    _goto(at, "Settings")
    assert _no_exc(at)
    assert "configured and ready" in _text(at)               # the shared availability hint

    _goto(at, "Assistant")
    assert _no_exc(at)
    assert "configured and ready" in _text(at)               # same fn → same message


# --------------------------------------------------------------------------- #
# 4) A confirmed material profile persists across messages + name changes (stable id)
# --------------------------------------------------------------------------- #
def test_confirmed_profile_persists_and_survives_name_change(usable_profile, release_model):
    s = agent_state.AgentState()
    orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min at 25 C",
                 material_profile=usable_profile, release_model=release_model)
    assert s.composition_usable
    pid = usable_profile.profile_id

    # A later message re-passing the profile keeps it (stable id, not name-matched).
    orch.respond(s, "also measure Si",
                 material_profile=usable_profile, release_model=release_model)
    assert s.composition_usable and s.material_profile.profile_id == pid

    # Renaming the material in conversation must NOT invalidate the confirmed profile.
    orch.respond(s, "call it just Class C fly ash",
                 material_profile=usable_profile, release_model=release_model)
    assert s.composition_usable and s.material_profile.profile_id == pid

    # Even a turn that does NOT re-pass the profile keeps it (attach_context never wipes on None).
    orch.respond(s, "what's next?")
    assert s.composition_usable and s.material_profile.profile_id == pid


# --------------------------------------------------------------------------- #
# 5) A chosen global 1% release model persists after it is acknowledged
# --------------------------------------------------------------------------- #
def test_global_release_model_persists(usable_profile):
    s = agent_state.AgentState()
    orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min",
                 material_profile=usable_profile, release_model=source_terms.global_release(0.01))
    assert s.release_model_status == agent_state.RELEASE_CHOSEN
    assert s.release_model.mode == source_terms.MODE_GLOBAL
    assert abs(s.release_model.global_fraction - 0.01) < 1e-9

    # A later turn that does not re-pass it keeps the chosen model.
    orch.respond(s, "also measure Si", material_profile=usable_profile)
    assert s.release_model_status == agent_state.RELEASE_CHOSEN
    assert s.release_model.global_fraction == 0.01


# --------------------------------------------------------------------------- #
# 6) Changing the release model invalidates the preview but keeps the composition
# --------------------------------------------------------------------------- #
def test_changing_release_invalidates_preview_keeps_composition(usable_profile):
    from flyash_phreeqc_ml.agent.agent_orchestrator import _invalidate_preview_if_stale
    s = agent_state.AgentState()
    orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min",
                 material_profile=usable_profile, release_model=source_terms.global_release(0.01))
    orch.respond(s, "build the preview", client=FakeClient([_action(A.BUILD_PHREEQC_PREVIEW)]),
                 material_profile=usable_profile, release_model=source_terms.global_release(0.01))
    assert s.preview is not None and s.preview_runnable

    # Change the release model (as the UI would) and invalidate: the preview clears with a clear
    # reason, but the confirmed composition is NOT erased.
    s.release_model = source_terms.global_release(0.05)
    note = _invalidate_preview_if_stale(s)
    assert s.preview is None and "release model" in note.lower()
    assert s.composition_usable                                # composition kept
    assert s.material_profile.profile_id == usable_profile.profile_id


def test_changing_release_via_turn_rebuilds_with_new_release(usable_profile):
    """End-to-end: changing the release model clears the stale preview AND immediately allows a
    rebuilt preview (composition still confirmed) — it does not forget the composition."""
    s = agent_state.AgentState()
    orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min",
                 material_profile=usable_profile, release_model=source_terms.global_release(0.01))
    orch.respond(s, "build the preview", client=FakeClient([_action(A.BUILD_PHREEQC_PREVIEW)]),
                 material_profile=usable_profile, release_model=source_terms.global_release(0.01))
    old_preview = s.preview
    r = orch.respond(s, "use a 5% release instead", material_profile=usable_profile,
                     release_model=source_terms.global_release(0.05))
    assert "release model" in r.assistant_message.lower()      # clear reason given
    assert s.composition_usable                                # composition kept
    assert s.preview is not None and s.preview is not old_preview    # rebuilt, not stale
    assert s.scenario_preview_signature()[1] == (source_terms.MODE_GLOBAL, 0.05)


# --------------------------------------------------------------------------- #
# 7) A ready preview is stored as the executable pending run
# --------------------------------------------------------------------------- #
def test_ready_preview_is_stored_as_pending_run(usable_profile, release_model):
    s = _ready_state(usable_profile, release_model)
    orch.respond(s, "build the preview", client=FakeClient([_action(A.BUILD_PHREEQC_PREVIEW)]),
                 material_profile=usable_profile, release_model=release_model)
    assert s.preview_status == "ready_for_review"
    assert s.run_lifecycle == agent_state.LIFECYCLE_READY_FOR_REVIEW

    r = orch.respond(s, "run it", client=FakeClient([_action(A.RUN_SINGLE_SIMULATION)]),
                     material_profile=usable_profile, release_model=release_model)
    assert r.awaiting_confirmation
    assert s.pending_action.action_name == A.RUN_SINGLE_SIMULATION
    assert s.pending_preview is s.preview                      # the exact reviewed input is stored
    assert s.run_lifecycle == agent_state.LIFECYCLE_AWAITING_CONFIRMATION


# --------------------------------------------------------------------------- #
# 8) Confirmation executes the stored pending preview instead of rebuilding
# --------------------------------------------------------------------------- #
def test_confirmation_runs_stored_preview_not_rebuild(usable_profile, release_model, monkeypatch):
    s = _ready_state(usable_profile, release_model)
    orch.respond(s, "build the preview", client=FakeClient([_action(A.BUILD_PHREEQC_PREVIEW)]),
                 material_profile=usable_profile, release_model=release_model)
    orch.respond(s, "run it", client=FakeClient([_action(A.RUN_SINGLE_SIMULATION)]),
                 material_profile=usable_profile, release_model=release_model)
    snapshot = s.pending_preview
    assert snapshot is not None

    # The builder must NOT be called during confirmation (no silent rebuild).
    import flyash_phreeqc_ml.agent.tool_registry as tr
    monkeypatch.setattr(tr.phreeqc_input_builder, "build_phreeqc_input_preview",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("rebuilt on confirm")))
    seen = {}

    def fake_run_and_parse(preview, **kw):
        seen["preview"] = preview
        return _ok_execution()(preview)

    monkeypatch.setattr(phreeqc_executor, "run_and_parse", fake_run_and_parse)

    r = orch.confirm_pending_action(s)
    assert r.executed is True
    assert seen["preview"] is snapshot                         # ran the stored snapshot
    assert s.execution_result.status == phreeqc_executor.STATUS_SUCCESS
    assert s.run_lifecycle == agent_state.LIFECYCLE_EXECUTED


# --------------------------------------------------------------------------- #
# 9) Missing PHREEQC gives a clear unavailable state without crashing
# --------------------------------------------------------------------------- #
def test_missing_phreeqc_is_clear_and_does_not_crash(usable_profile, release_model, monkeypatch):
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", "definitely_not_a_real_binary_xyz")
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    s = _ready_state(usable_profile, release_model)
    orch.respond(s, "build the preview", client=FakeClient([_action(A.BUILD_PHREEQC_PREVIEW)]),
                 material_profile=usable_profile, release_model=release_model)
    orch.respond(s, "run it", client=FakeClient([_action(A.RUN_SINGLE_SIMULATION)]),
                 material_profile=usable_profile, release_model=release_model)
    r = orch.confirm_pending_action(s)                         # executes the (gated) run

    assert s.execution_result is not None                      # no crash, structured result
    assert s.execution_result.status == phreeqc_executor.STATUS_MISSING
    assert s.run_lifecycle == agent_state.LIFECYCLE_FAILED
    msg = r.assistant_message.lower()
    assert "phreeqc" in msg and "preview" in msg               # clear, honest, actionable


# --------------------------------------------------------------------------- #
# 10) A completed PHREEQC result is visible in the Assistant AND the Results tab
# --------------------------------------------------------------------------- #
def test_result_visible_in_assistant_and_results(monkeypatch, tmp_path):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    from ui import assistant_tab
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")

    state = agent_state.AgentState()
    state.domain = domains.LEACHING_GEOCHEMISTRY
    state.engine = domains.engine_for(domains.LEACHING_GEOCHEMISTRY)
    state.execution_result = phreeqc_executor.ExecutionResult(
        "ASSISTANT-001", phreeqc_executor.STATUS_SUCCESS, output_path="x.pqo")
    state.parsed_result = phreeqc_executor.ParsedSimulation(
        "ASSISTANT-001", phreeqc_executor.PARSE_PARSED, pH=13.48,
        element_totals_mM={"Ca": 5.0, "Si": 2.0})
    key = assistant_tab._state_key(None)

    at = AppTest.from_file("app.py", default_timeout=120).run()
    at.session_state[key] = state

    _goto(at, "Assistant")
    assert _no_exc(at)
    assert "13.48" in _text(at)                                # inline result in the Assistant

    _goto(at, "Results")
    assert _no_exc(at)
    assert "13.48" in _text(at)                                # same result in the Results section


# --------------------------------------------------------------------------- #
# 11) "0.5 M" is never parsed or displayed as "5 M"
# --------------------------------------------------------------------------- #
def test_naoh_half_molar_not_five_molar(usable_profile, release_model):
    # Clean phrasing.
    s = agent_state.AgentState()
    orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH for 60 min")
    assert s.scenario.leachant.leachant_concentration_M == 0.5

    # Messy demo phrasing (".5m") — the concentration is 0.5 M, not 5 M (the L/S ratio is 5.0,
    # which is correct and unrelated).
    s2 = _ready_state(usable_profile, release_model)
    assert s2.scenario.leachant.leachant_concentration_M == 0.5
    leachant_var = next((v for v in (s2.last_understanding.get("key_variables") or [])
                         if "naoh" in v.lower() or "concentration" in v.lower()), "")
    assert "5 M" not in leachant_var and "5.0 M" not in leachant_var

    # The generated preview text shows 0.5 M, never a bare 5 M.
    orch.respond(s2, "build the preview", client=FakeClient([_action(A.BUILD_PHREEQC_PREVIEW)]),
                 material_profile=usable_profile, release_model=release_model)
    text = s2.preview.phreeqc_input_text
    assert "NaOH 0.5 M" in text
    assert "NaOH 5 M" not in text and "NaOH 5.0 M" not in text


# --------------------------------------------------------------------------- #
# 12) No API key / secret appears in messages, provenance, or committed source
# --------------------------------------------------------------------------- #
def test_no_api_key_in_status_messages_or_provenance(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, FAKE_KEY)
    cfg = ai_config.resolve_config()
    assert cfg.key_present is True
    assert FAKE_KEY not in cfg.status_line()
    assert FAKE_KEY not in json.dumps(cfg.to_safe_dict())
    assert FAKE_KEY not in ai_config.live_ai_status(cfg, True).reason

    s = agent_state.AgentState()
    r = orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH")     # deterministic (no client)
    blob = json.dumps([m.to_dict() for m in s.history]
                      + [e.to_dict() for e in s.provenance]
                      + [s.to_provenance_dict()])
    assert FAKE_KEY not in blob and FAKE_KEY not in r.assistant_message


def test_no_real_api_key_committed_in_source():
    """No real Anthropic key prefix anywhere in the project source. The fake test key uses a
    clearly-non-real 'TESTKEY' marker; the real-key needle is built from parts so this scanner
    file itself never contains the contiguous literal it searches for."""
    needle = "sk-ant-" + "api03-"                               # the real production-key prefix
    root = Path(__file__).resolve().parent.parent
    skip_dirs = {".venv", ".git", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}
    exts = {".py", ".md", ".toml", ".sh", ".yml", ".yaml", ".txt", ".cfg", ".json"}
    offenders = []
    for p in root.rglob("*"):
        if p.is_dir() or any(part in skip_dirs for part in p.parts):
            continue
        if p.suffix.lower() not in exts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:                                       # noqa: BLE001
            continue
        if needle in text:
            offenders.append(str(p.relative_to(root)))
    assert not offenders, f"a real-looking API key was found in: {offenders}"


# --------------------------------------------------------------------------- #
# 13) Chat-typed composition auto-fills the Advanced-details state (UI wiring)
# --------------------------------------------------------------------------- #
def test_chat_composition_autofills_advanced_details_in_ui(monkeypatch, tmp_path):
    """End-to-end at the UI level: typing a composition in the Assistant chat populates the
    canonical material-profile state (the same key the Advanced-details expander + the builder
    read), as a draft — proving the chat and the Advanced options now share one source of truth."""
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    from flyash_phreeqc_ml.materials import profile_schema as mp
    from ui import assistant_tab
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")

    at = AppTest.from_file("app.py", default_timeout=120).run()
    _goto(at, "Assistant")
    assert _no_exc(at)

    prompt = ("im leaching class c fly ash with naoh. use composition sio2 34 al2o3 18 cao 24 "
              "fe2o3 7 mgo 5 na2o 2 k2o 1 so3 4 loi other 5. use global 1% release. use phreeqc.dat")
    at.chat_input[0].set_value(prompt).run()
    assert _no_exc(at)

    # The canonical material-profile state holds the chat-parsed DRAFT (9 components), unconfirmed.
    profile = at.session_state[assistant_tab._state_key(None)].material_profile
    assert profile is not None and len(profile.entries) == 9
    assert profile.verification_status == mp.STATUS_DRAFT and not profile.is_usable
    # The per-run session key the Advanced-details expander reads holds the same object.
    assert at.session_state["asst_mp___none_"] is profile
    # The release model + database were captured too.
    state = at.session_state[assistant_tab._state_key(None)]
    assert state.release_model is not None and state.requested_database == "phreeqc.dat"
    # The visible reply + next-step point the user at reviewing/confirming the parsed composition.
    assert "confirm" in _text(at).lower()


# --------------------------------------------------------------------------- #
# AppTest helpers
# --------------------------------------------------------------------------- #
def _goto(at, section):
    [r for r in at.radio if getattr(r, "key", None) == "nav_section"][0].set_value(section).run()
    return at


def _no_exc(at) -> bool:
    return at.exception is None or len(at.exception) == 0


def _text(at) -> str:
    parts: list = []
    for attr in ("markdown", "caption", "warning", "error", "info", "success",
                 "title", "header", "subheader"):
        for e in getattr(at, attr, []) or []:
            parts.append(str(getattr(e, "value", getattr(e, "body", ""))))
    return " ".join(parts)
