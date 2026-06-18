"""UI pins for the Settings 'Enable live AI assistant' toggle + Assistant gating.

Driven through the Streamlit AppTest harness (app.py is a script). These verify the user-visible
enable flow the config layer alone can't:

* the toggle appears + is operable when a key + the SDK are present, disabled with an explanation
  otherwise, and the API key value is never rendered;
* **the toggle persists across section navigation AND prompt submission** — the regression for the
  "AI turns itself off after I submit" bug (the choice is stored in a plain session key, not the
  toggle's widget key, so Streamlit's widget-state GC on navigation can't drop it);
* the Assistant passes ``use_ai=True`` only when the toggle is on, else deterministic;
* a live-AI **failure** surfaces a one-turn fallback warning and **never disables the toggle**.

No real AI call is made (the orchestrator or the AI client is mocked).
"""
from __future__ import annotations

import types

import pytest

from flyash_phreeqc_ml.agent import agent_orchestrator
from flyash_phreeqc_ml.ai import config as ai_config

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = "app.py"
PERSIST_KEY = "ai_live_enabled"            # the plain key the Assistant reads (survives reruns)
WIDGET_KEY = "ai_live_enabled__toggle"     # the Settings toggle widget's own key
SECRET = "sk-ant-TESTKEY-not-real-do-not-leak-0001"


@pytest.fixture(autouse=True)
def _ai_env(monkeypatch):
    """Start every test env-only with no Streamlit secrets and no runtime overrides."""
    for name in (ai_config.API_KEY_ENV, ai_config.MODEL_ENV, ai_config.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    ai_config.clear_runtime_overrides()
    monkeypatch.setattr(ai_config, "_secrets_get", lambda name: None)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)   # SDK genuinely installed
    yield
    ai_config.clear_runtime_overrides()


def _nav(at, section):
    [r for r in at.radio if getattr(r, "key", None) == "nav_section"][0].set_value(section).run()
    return at


def _widget(at, key):
    matches = [t for t in at.toggle if getattr(t, "key", None) == key]
    return matches[0] if matches else None


def _ss(at, key, default="<MISSING>"):
    try:
        return at.session_state[key]
    except Exception:
        return default


def _rendered_text(at) -> str:
    parts = [str(m.value) for m in at.markdown]
    for attr in ("warning", "error", "info", "success"):
        parts += [str(getattr(e, "value", "")) for e in getattr(at, attr, [])]
    for t in at.toggle:
        parts += [str(getattr(t, "label", "")), str(getattr(t, "help", ""))]
    return " ".join(parts)


def _spy(captured):
    def respond(state, message, **kwargs):
        captured["use_ai"] = kwargs.get("use_ai")
        captured["message"] = message
        return types.SimpleNamespace(state=state, assistant_message="ok", action=None,
                                     council=None, executed=False, awaiting_confirmation=False)
    return respond


# --------------------------------------------------------------------------- #
# The toggle exists + is operable when a key is detected; key never rendered
# --------------------------------------------------------------------------- #
def test_settings_exposes_enable_toggle_when_key_detected(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    at = AppTest.from_file(APP, default_timeout=90).run()
    _nav(at, "Settings")
    assert at.exception is None or len(at.exception) == 0
    tog = _widget(at, WIDGET_KEY)
    assert tog is not None, "the 'Enable live AI assistant' toggle is missing from Settings"
    assert tog.disabled is False, "toggle should be operable when key + SDK are present"
    assert tog.label == "Enable live AI assistant"
    assert SECRET not in _rendered_text(at)


def test_settings_status_card_reports_capability(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    at = AppTest.from_file(APP, default_timeout=90).run()
    _nav(at, "Settings")
    text = _rendered_text(at)
    assert "Detected" in text and "Available" in text       # API key + AI SDK cards
    assert SECRET not in text


def test_toggle_disabled_without_key():
    at = AppTest.from_file(APP, default_timeout=90).run()
    _nav(at, "Settings")
    assert at.exception is None or len(at.exception) == 0
    tog = _widget(at, WIDGET_KEY)
    assert tog is not None and tog.disabled is True
    assert ai_config.API_KEY_ENV in _rendered_text(at)       # tells the user how to enable it


def test_toggle_on_syncs_to_persistent_key(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    at = AppTest.from_file(APP, default_timeout=90).run()
    _nav(at, "Settings")
    _widget(at, WIDGET_KEY).set_value(True).run()
    assert _ss(at, PERSIST_KEY) is True                      # widget synced into the plain key
    assert SECRET not in _rendered_text(at)


# --------------------------------------------------------------------------- #
# REGRESSION: the toggle survives navigation + prompt submission
# --------------------------------------------------------------------------- #
def test_toggle_persists_across_navigation_and_submit(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    captured: dict = {}
    monkeypatch.setattr(agent_orchestrator, "respond", _spy(captured))
    at = AppTest.from_file(APP, default_timeout=90).run()

    _nav(at, "Settings")
    _widget(at, WIDGET_KEY).set_value(True).run()
    assert _ss(at, PERSIST_KEY) is True

    _nav(at, "Assistant")
    assert _ss(at, PERSIST_KEY) is True                      # survived navigation (was the bug)

    at.chat_input[0].set_value("im leeching class c fli ash w naoh .5m 2g 10ml").run()
    assert _ss(at, PERSIST_KEY) is True                      # survived submission (was the bug)
    assert captured.get("use_ai") is True                    # AI actually used after submit


# --------------------------------------------------------------------------- #
# Assistant gating: live AI only when the toggle is on (orchestrator mocked)
# --------------------------------------------------------------------------- #
def test_assistant_uses_live_ai_when_toggle_on(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    captured: dict = {}
    monkeypatch.setattr(agent_orchestrator, "respond", _spy(captured))
    at = AppTest.from_file(APP, default_timeout=90).run()
    at.session_state[PERSIST_KEY] = True
    _nav(at, "Assistant")
    at.chat_input[0].set_value("im leeching class c fli ash w naoh .5m 2g 10ml").run()
    assert captured.get("use_ai") is True


def test_assistant_deterministic_when_toggle_off(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    captured: dict = {}
    monkeypatch.setattr(agent_orchestrator, "respond", _spy(captured))
    at = AppTest.from_file(APP, default_timeout=90).run()
    _nav(at, "Assistant")                                    # toggle left off (default)
    at.chat_input[0].set_value("im leeching class c fli ash w naoh .5m 2g 10ml").run()
    assert captured.get("use_ai") is False


def test_assistant_deterministic_without_key(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(agent_orchestrator, "respond", _spy(captured))
    at = AppTest.from_file(APP, default_timeout=90).run()
    at.session_state[PERSIST_KEY] = True                     # stale on, but no key
    _nav(at, "Assistant")
    at.chat_input[0].set_value("hello").run()
    assert captured.get("use_ai") is False


# --------------------------------------------------------------------------- #
# A live-AI failure surfaces a one-turn warning and NEVER disables the toggle
# --------------------------------------------------------------------------- #
def test_ai_failure_falls_back_one_turn_without_disabling_toggle(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    # Make every AI client request fail → the NLU + council fall back deterministically (no network).
    from flyash_phreeqc_ml.ai import client as ai_client
    monkeypatch.setattr(ai_client, "get_client",
                        lambda *a, **k: types.SimpleNamespace(ok=False, client=None,
                                                              error_code="sim", message="sim fail"))
    at = AppTest.from_file(APP, default_timeout=120).run()
    at.session_state[PERSIST_KEY] = True                     # live AI ON
    _nav(at, "Assistant")
    at.chat_input[0].set_value("im leeching class c fli ash w naoh .5m 2g 10ml").run()

    assert at.exception is None or len(at.exception) == 0
    assert _ss(at, PERSIST_KEY) is True                      # the failure did NOT disable the toggle
    text = _rendered_text(at).lower()
    assert "unavailable for this message" in text            # the failure is surfaced, not silent
