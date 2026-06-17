"""Shared UI render helpers used by more than one tab (next-step hint, valid-now
section, mapping-status definitions, provenance captions, audit-once).

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

from pathlib import Path
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import app_ui  # noqa: E402  (presentation-only UI helper layer)
from flyash_phreeqc_ml import replicates  # noqa: E402

from ui.state import _NOT_VALID_YET, _VALID_NOW, _next_step_hint, _rel

def _png_provenance_caption(path: Path, kind: str) -> str:
    """Provenance line for a static PNG result figure: source path + generated time + type +
    the fact that the Simulate tab does not regenerate it. (UI label only.)"""
    try:
        ts = pd.Timestamp.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        ts = "unknown"
    return (f"Source figure: `{_rel(path)}` · generated {ts} · {kind} · static image — "
            "regenerated only by re-running the workflow, **not** by the Simulate tab.")

def _render_mapping_status_definitions() -> None:
    """Feature 2 — the four mapping statuses and what they mean."""
    rows = [{"status": k, "meaning": v}
            for k, v in replicates.MAPPING_STATUS_DEFINITIONS.items()]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=180, hide_index=True)

def _render_valid_now_section() -> None:
    """Feature 5 — what is scientifically valid now vs not fully valid yet."""
    a, b = st.columns(2)
    with a:
        with st.container(border=True):
            app_ui.render_status_badge("Valid now", "valid")
            st.markdown("\n".join(f"- {x}" for x in _VALID_NOW))
    with b:
        with st.container(border=True):
            app_ui.render_status_badge("Not fully valid yet", "preliminary")
            st.markdown("\n".join(f"- {x}" for x in _NOT_VALID_YET))

def _render_next_step(selected_run: str | None) -> None:
    """Render the one-line "next step" hint at the top of a tab."""
    st.info(f"➡️ **Next step:** {_next_step_hint(selected_run)}")

def _audit_once(run_name: str, dedupe_key: str, log_fn) -> None:
    """Log a render-time event at most once per (run, key) per session (no spam).

    Render functions run on every interaction; ``dedupe_key`` should encode the
    salient state (e.g. the counts) so a *genuine* change logs again but a re-render
    of the same state does not.
    """
    if not run_name:
        return
    seen = st.session_state.setdefault("_audit_seen", set())
    token = (run_name, dedupe_key)
    if token in seen:
        return
    seen.add(token)
    try:
        log_fn()
    except Exception:  # pragma: no cover - logging must never crash a render
        pass
