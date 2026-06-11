"""Shared UI/UX layer for the Streamlit app — presentation only.

This module is pure visual scaffolding: a global stylesheet plus small helpers
for page headers, workflow steppers, status badges, metric/section cards, and
styled callout panels. It contains **no chemistry, no ML, and no pipeline
logic** — every helper only *formats* values that ``app.py`` has already
computed. Keeping it separate lets ``app.py`` stay focused on wiring package
functions to widgets while the look-and-feel lives in one place.

Design goals (a clean academic research dashboard):

* minimal, high-end, lots of whitespace, consistent typography;
* card-based layout with consistent section headings;
* one consistent status-colour system shared by badges, cards and steppers
  (``exact`` = green, ``scenario-level`` = amber, ``unsafe`` = red,
  ``needs new simulation`` = blue/purple, ``preliminary`` = amber);
* theme-agnostic colours (translucent greys + colour tints) so it reads on
  both the light and dark Streamlit themes.

Nothing here changes scientific wording or removes any functionality — it is a
styling layer the tab renderers opt into.
"""
from __future__ import annotations

import html as _html
from typing import Iterable, Sequence

import streamlit as st

# --------------------------------------------------------------------------- #
# Status colour system — one source of truth for every status colour in the UI.
# Each entry: status keyword -> (foreground colour, translucent background tint).
# Translucent tints keep the palette readable on both light and dark themes.
# --------------------------------------------------------------------------- #
_GREEN = "#1a8f5a"
_AMBER = "#b67611"
_RED = "#d0402b"
_PURPLE = "#6a4bd0"
_BLUE = "#2f6fb0"
_NEUTRAL = "#6b7280"

STATUS_STYLES: dict[str, tuple[str, str]] = {
    # Canonical mapping statuses (from replicates.MAPPING_STATUS_*).
    "exact": (_GREEN, "rgba(26,143,90,.12)"),
    "scenario-level only": (_AMBER, "rgba(182,118,17,.14)"),
    "scenario-level": (_AMBER, "rgba(182,118,17,.14)"),
    "unsafe": (_RED, "rgba(208,64,43,.12)"),
    "needs new simulation": (_PURPLE, "rgba(106,75,208,.12)"),
    # Validation / generic statuses.
    "valid": (_GREEN, "rgba(26,143,90,.12)"),
    "preliminary": (_AMBER, "rgba(182,118,17,.14)"),
    "single-sample": (_AMBER, "rgba(182,118,17,.14)"),
    "needs new simulations": (_PURPLE, "rgba(106,75,208,.12)"),
    "nothing to compare": (_NEUTRAL, "rgba(107,114,128,.12)"),
    # Generic severities (map onto Streamlit's success/warning/error/info).
    "ok": (_GREEN, "rgba(26,143,90,.12)"),
    "success": (_GREEN, "rgba(26,143,90,.12)"),
    "good": (_GREEN, "rgba(26,143,90,.12)"),
    "warning": (_AMBER, "rgba(182,118,17,.14)"),
    "error": (_RED, "rgba(208,64,43,.12)"),
    "danger": (_RED, "rgba(208,64,43,.12)"),
    "info": (_BLUE, "rgba(47,111,176,.12)"),
    "neutral": (_NEUTRAL, "rgba(107,114,128,.12)"),
    "muted": (_NEUTRAL, "rgba(107,114,128,.12)"),
}


def _style_for(status: str | None) -> tuple[str, str]:
    """Return ``(colour, tint)`` for a status keyword (falls back to neutral)."""
    key = (status or "").strip().lower()
    return STATUS_STYLES.get(key, STATUS_STYLES["neutral"])


# --------------------------------------------------------------------------- #
# Global stylesheet — injected once per session.
# --------------------------------------------------------------------------- #
_GLOBAL_CSS = """
<style id="rd-theme">
/* ---- Typography & layout rhythm ---------------------------------------- */
html, body, [class*="css"] {
  font-feature-settings: "kern" 1, "liga" 1;
}
.block-container { padding-top: 2.4rem; padding-bottom: 3rem; max-width: 1280px; }
[data-testid="stHeader"] { background: transparent; }

/* ---- Hero header ------------------------------------------------------- */
.rd-hero {
  border: 1px solid rgba(128,128,128,.22);
  border-radius: 16px;
  padding: 22px 26px;
  margin-bottom: 8px;
  background:
    linear-gradient(135deg, rgba(47,111,176,.10), rgba(106,75,208,.06) 60%, transparent);
}
.rd-hero-eyebrow {
  font-size: .72rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase;
  opacity: .70; margin-bottom: 6px;
}
.rd-hero-title { font-size: 1.7rem; font-weight: 700; line-height: 1.2; margin: 0; }
.rd-hero-sub { font-size: 1rem; opacity: .80; margin-top: 6px; max-width: 70ch; }
.rd-hero-chips { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }

/* ---- Page header (per tab) -------------------------------------------- */
.rd-page-eyebrow {
  font-size: .70rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase;
  opacity: .60;
}
.rd-page-title { font-size: 1.35rem; font-weight: 700; line-height: 1.2; margin: 2px 0 0 0; }
.rd-page-sub { font-size: .95rem; opacity: .78; margin: 4px 0 2px 0; max-width: 76ch; }

/* ---- Section heading --------------------------------------------------- */
.rd-section {
  display: flex; align-items: baseline; gap: 10px;
  margin: 6px 0 2px 0;
}
.rd-section-title { font-size: 1.05rem; font-weight: 650; }
.rd-section-sub { font-size: .85rem; opacity: .65; }

/* ---- Badges (status pills) -------------------------------------------- */
.rd-badge {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: .76rem; font-weight: 650; letter-spacing: .01em; white-space: nowrap;
  line-height: 1.5;
}

/* ---- Metric / status cards (HTML grid) -------------------------------- */
.rd-card-grid {
  display: grid; gap: 12px; margin: 6px 0 4px 0;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}
.rd-card {
  border: 1px solid rgba(128,128,128,.20);
  border-left-width: 4px;
  border-radius: 12px;
  padding: 12px 14px;
  background: rgba(128,128,128,.05);
}
.rd-card-label {
  font-size: .72rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  opacity: .66;
}
.rd-card-value { font-size: 1.5rem; font-weight: 700; line-height: 1.25; margin-top: 2px; }
.rd-card-caption { font-size: .76rem; opacity: .66; margin-top: 2px; }

/* ---- Native st.metric -> card (upgrades existing call sites) ----------- */
[data-testid="stMetric"] {
  border: 1px solid rgba(128,128,128,.20);
  border-radius: 12px;
  padding: 12px 14px;
  background: rgba(128,128,128,.05);
}
[data-testid="stMetricLabel"] p {
  font-size: .72rem !important; font-weight: 600; letter-spacing: .03em;
  text-transform: uppercase; opacity: .68;
}
[data-testid="stMetricValue"] { font-size: 1.5rem; font-weight: 700; }

/* ---- Callout panels ---------------------------------------------------- */
.rd-panel {
  border: 1px solid var(--rd-c);
  border-left-width: 4px;
  border-radius: 12px;
  padding: 12px 16px; margin: 6px 0;
  background: var(--rd-bg);
}
.rd-panel-title { font-weight: 650; margin-bottom: 2px; }
.rd-panel-body { font-size: .9rem; opacity: .92; }

/* ---- Workflow stepper -------------------------------------------------- */
.rd-steps { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 4px 0 8px 0; }
.rd-step {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 5px 12px; border-radius: 999px; font-size: .82rem; font-weight: 550;
  border: 1px solid rgba(128,128,128,.22); background: rgba(128,128,128,.05); opacity: .72;
}
.rd-step .rd-step-n {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 999px; font-size: .70rem; font-weight: 700;
  background: rgba(128,128,128,.22);
}
.rd-step-done { opacity: .9; border-color: rgba(26,143,90,.45); }
.rd-step-done .rd-step-n { background: rgba(26,143,90,.85); color: #fff; }
.rd-step-current {
  opacity: 1; border-color: rgba(47,111,176,.55); background: rgba(47,111,176,.12);
  font-weight: 650;
}
.rd-step-current .rd-step-n { background: #2f6fb0; color: #fff; }
.rd-step-sep { opacity: .35; font-size: .8rem; }

/* ---- Tabs -------------------------------------------------------------- */
.stTabs [data-baseweb="tab-list"] { gap: 4px; }
.stTabs [data-baseweb="tab"] {
  border-radius: 10px 10px 0 0; padding: 8px 16px; font-weight: 550;
}

/* ---- Expanders --------------------------------------------------------- */
[data-testid="stExpander"] {
  border: 1px solid rgba(128,128,128,.18); border-radius: 12px;
}

/* ---- Bordered containers (st.container(border=True)) ------------------- */
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 12px; }

/* ---- Dividers a touch lighter ----------------------------------------- */
hr { opacity: .45; }
</style>
"""


def inject_global_css() -> None:
    """Inject the global stylesheet once per session (idempotent)."""
    if st.session_state.get("_rd_css_done"):
        return
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)
    st.session_state["_rd_css_done"] = True


# --------------------------------------------------------------------------- #
# Header helpers
# --------------------------------------------------------------------------- #
def render_hero(title: str, subtitle: str, *, eyebrow: str | None = None,
                chips: Sequence[tuple[str, str]] | None = None) -> None:
    """Polished top-of-app hero banner.

    ``chips`` is an optional sequence of ``(label, status)`` rendered as badges
    (e.g. project context). All text is HTML-escaped.
    """
    parts = ['<div class="rd-hero">']
    if eyebrow:
        parts.append(f'<div class="rd-hero-eyebrow">{_html.escape(eyebrow)}</div>')
    parts.append(f'<div class="rd-hero-title">{_html.escape(title)}</div>')
    parts.append(f'<div class="rd-hero-sub">{_html.escape(subtitle)}</div>')
    if chips:
        chip_html = "".join(status_badge(lbl, status) for lbl, status in chips)
        parts.append(f'<div class="rd-hero-chips">{chip_html}</div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def render_page_header(title: str, subtitle: str, *, eyebrow: str | None = None) -> None:
    """Consistent per-tab header: small eyebrow, title, one-sentence purpose."""
    parts = ['<div class="rd-page-header">']
    if eyebrow:
        parts.append(f'<div class="rd-page-eyebrow">{_html.escape(eyebrow)}</div>')
    parts.append(f'<div class="rd-page-title">{_html.escape(title)}</div>')
    parts.append(f'<div class="rd-page-sub">{_html.escape(subtitle)}</div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def section_header(title: str, subtitle: str | None = None) -> None:
    """A consistent lightweight section heading (title + optional muted note)."""
    sub = f'<span class="rd-section-sub">{_html.escape(subtitle)}</span>' if subtitle else ""
    st.markdown(
        f'<div class="rd-section"><span class="rd-section-title">'
        f'{_html.escape(title)}</span>{sub}</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Badges
# --------------------------------------------------------------------------- #
def status_badge(label: str, status: str | None = None) -> str:
    """Return an inline HTML badge string coloured by ``status``.

    ``status`` defaults to ``label`` so ``status_badge("exact")`` just works for
    the canonical mapping statuses; pass an explicit ``status`` to colour an
    arbitrary label (e.g. ``status_badge("3 unsafe", "unsafe")``).
    """
    colour, tint = _style_for(status if status is not None else label)
    return (f'<span class="rd-badge" style="color:{colour};background:{tint}">'
            f'{_html.escape(str(label))}</span>')


def render_status_badge(label: str, status: str | None = None) -> None:
    """Render a standalone status badge (thin wrapper over :func:`status_badge`)."""
    st.markdown(status_badge(label, status), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def _card_html(label, value, caption=None, status=None) -> str:
    colour, _tint = _style_for(status)
    cap = (f'<div class="rd-card-caption">{_html.escape(str(caption))}</div>'
           if caption not in (None, "") else "")
    border = f"border-left-color:{colour};" if status else ""
    return (f'<div class="rd-card" style="{border}">'
            f'<div class="rd-card-label">{_html.escape(str(label))}</div>'
            f'<div class="rd-card-value">{_html.escape(str(value))}</div>'
            f'{cap}</div>')


def render_metric_cards(items: Iterable[dict]) -> None:
    """Render a responsive grid of status-coloured metric cards.

    Each item is a dict with ``label`` and ``value`` (required) and optional
    ``caption`` and ``status`` (status colours the left accent border). Use this
    for the prominent at-a-glance summary rows; plain ``st.metric`` is fine
    elsewhere (the global CSS already styles those as cards too).
    """
    cards = "".join(
        _card_html(it.get("label", ""), it.get("value", ""),
                   it.get("caption"), it.get("status"))
        for it in items
    )
    st.markdown(f'<div class="rd-card-grid">{cards}</div>', unsafe_allow_html=True)


def render_section_card(title: str, body_md: str, *, status: str | None = None) -> None:
    """A simple bordered text card (title + markdown body) for short reference blurbs."""
    colour, tint = _style_for(status or "neutral")
    st.markdown(
        f'<div class="rd-panel" style="--rd-c:{colour};--rd-bg:{tint}">'
        f'<div class="rd-panel-title">{_html.escape(title)}</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(body_md)


def render_warning_panel(title: str, message: str, *, level: str = "warning") -> None:
    """A styled callout panel (info/success/warning/error) with a title + body.

    Use for emphasis that should read as a coloured card rather than a default
    Streamlit alert. ``level`` keys into the shared status palette.
    """
    colour, tint = _style_for(level)
    st.markdown(
        f'<div class="rd-panel" style="--rd-c:{colour};--rd-bg:{tint}">'
        f'<div class="rd-panel-title">{_html.escape(title)}</div>'
        f'<div class="rd-panel-body">{_html.escape(message)}</div></div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Workflow stepper
# --------------------------------------------------------------------------- #
def render_workflow_steps(steps: Sequence[str], current: int | None = None) -> None:
    """Render a horizontal workflow stepper.

    ``steps`` are short labels; ``current`` is the 0-based index of the active
    step (earlier steps render as *done*, later ones as *upcoming*). Pass
    ``current=None`` for a neutral, non-highlighted overview of the workflow.
    """
    chunks: list[str] = ['<div class="rd-steps">']
    for i, label in enumerate(steps):
        if current is None:
            cls = ""
        elif i < current:
            cls = " rd-step-done"
        elif i == current:
            cls = " rd-step-current"
        else:
            cls = ""
        chunks.append(
            f'<span class="rd-step{cls}"><span class="rd-step-n">{i + 1}</span>'
            f'{_html.escape(label)}</span>'
        )
        if i < len(steps) - 1:
            chunks.append('<span class="rd-step-sep">→</span>')
    chunks.append("</div>")
    st.markdown("".join(chunks), unsafe_allow_html=True)


def advanced_expander(title: str, *, expanded: bool = False):
    """Consistent 'advanced details' expander (kept collapsed by default).

    A thin wrapper that prefixes a gear glyph so advanced/debug sections read
    consistently across tabs. Returns the expander context manager.
    """
    return st.expander(f"⚙︎ {title}", expanded=expanded)
