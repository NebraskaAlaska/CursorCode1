"""Engine Library section — the modular engine registry (presentation only).

Makes the broad-materials identity concrete: PHREEQC is **one** engine in a modular library,
not the whole app. Shows which engines are **executable now**, which domains get **planning
support now**, and which engines are **future**. Reads `domains.engine_status()` / the planning
metadata; owns no chemistry and no result-path logic.

One of the seven top-level sections (Assistant · Workspace · Results · Data & Validation ·
Projects · Engine Library · Settings).
"""
from __future__ import annotations

import streamlit as st

import app_ui
from flyash_phreeqc_ml.agent import domains
from flyash_phreeqc_ml.simulation import phreeqc_executor


def _render_engine_library(selected_run: str | None) -> None:
    app_ui.render_page_header(
        "Engine Library",
        "A modular library of modelling engines. PHREEQC (leaching / geochemistry) is the first "
        "executable engine — more plug in behind the same assistant + confirmation flow.",
        eyebrow="One engine in a library · not the whole app")

    app_ui.render_engine_cards(domains.engine_status())

    st.divider()
    app_ui.section_header("Available now — PHREEQC")
    av = phreeqc_executor.check_availability()
    with st.container(border=True):
        st.markdown("**Leaching / dissolution / geochemistry**")
        app_ui.render_status_badge(
            "executable" + ("" if av.can_run else " — not configured on this machine"),
            "success" if av.can_run else "warning")
        st.markdown("- **Engine:** PHREEQC (aqueous speciation / equilibrium)\n"
                    "- **Status:** executable (runs after you confirm)\n"
                    "- **Mature demo:** Class C fly ash alkaline (NaOH) leaching\n"
                    "- Also supports other leaching systems (e.g. red mud + acid) — the "
                    "composition, release model, and database assumptions matter.")
        st.caption(av.message)

    app_ui.section_header("Planning support now",
                          "no executable engine yet — structured plans + data templates")
    cols = st.columns(2)
    planning = [
        ("Polymer composites / mechanical testing", domains.POLYMER_COMPOSITE),
        ("Thermal treatment / calcination", domains.THERMAL_TREATMENT),
        ("Cementitious binder formulation", domains.CEMENTITIOUS_BINDER),
        ("Battery / corrosion materials", domains.BATTERY_MATERIAL),
    ]
    for i, (label, dom) in enumerate(planning):
        with cols[i % 2]:
            with st.container(border=True):
                support = domains.planning_support(dom)
                st.markdown(f"**{label}**")
                app_ui.render_status_badge("planning support", "warning")
                st.caption("Measure: " + ", ".join(support["response_variables"][:5]))
                st.caption(f"Future engine: {support['future_engine']}.")

    app_ui.section_header("Future engines (modular)")
    for e in domains.FUTURE_ENGINES:
        st.markdown(f"- {e}")
    st.caption("Plus MatterSim / atomistic engines and a calibration/validation agent. Each "
               "registers per domain behind the same policy gate — see the architecture note in "
               "**Settings** and `docs/ai_architecture.md`.")


# The app dispatches to ``render``.
render = _render_engine_library
