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
# Apple/Squarespace-inspired system palette (see .streamlit/config.toml for the base theme).
_GREEN = "#1f8f43"   # success (text-readable variant of #34C759)
_AMBER = "#b3650a"   # warning (text-readable variant of #FF9500)
_RED = "#d6332a"     # danger  (text-readable variant of #FF3B30)
_PURPLE = "#5856D6"  # Apple system indigo
_BLUE = "#0066d6"    # accent  (text-readable variant of #007AFF)
_NEUTRAL = "#6E6E73"  # Apple secondary label

STATUS_STYLES: dict[str, tuple[str, str]] = {
    # Canonical mapping statuses (from replicates.MAPPING_STATUS_*).
    "exact": (_GREEN, "rgba(52,199,89,.12)"),
    "scenario-level only": (_AMBER, "rgba(255,149,0,.13)"),
    "scenario-level": (_AMBER, "rgba(255,149,0,.13)"),
    "unsafe": (_RED, "rgba(255,59,48,.11)"),
    "needs new simulation": (_PURPLE, "rgba(88,86,214,.11)"),
    # Validation / generic statuses.
    "valid": (_GREEN, "rgba(52,199,89,.12)"),
    "preliminary": (_AMBER, "rgba(255,149,0,.13)"),
    "single-sample": (_AMBER, "rgba(255,149,0,.13)"),
    "needs new simulations": (_PURPLE, "rgba(88,86,214,.11)"),
    "nothing to compare": (_NEUTRAL, "rgba(110,110,115,.10)"),
    # Generic severities (map onto Streamlit's success/warning/error/info).
    "ok": (_GREEN, "rgba(52,199,89,.12)"),
    "success": (_GREEN, "rgba(52,199,89,.12)"),
    "good": (_GREEN, "rgba(52,199,89,.12)"),
    "warning": (_AMBER, "rgba(255,149,0,.13)"),
    "error": (_RED, "rgba(255,59,48,.11)"),
    "danger": (_RED, "rgba(255,59,48,.11)"),
    "info": (_BLUE, "rgba(0,122,255,.11)"),
    "accent": (_BLUE, "rgba(0,122,255,.11)"),
    "neutral": (_NEUTRAL, "rgba(110,110,115,.10)"),
    "muted": (_NEUTRAL, "rgba(110,110,115,.10)"),
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
/* ===== Materials Research Assistant — Apple/Squarespace-inspired design system =====
   Light neutral background, white cards, rounded corners, restrained accent, minimal
   borders, clear type hierarchy. Tokens mirror .streamlit/config.toml. ===== */
:root {
  --rd-bg: #F5F5F7;
  --rd-card: #FFFFFF;
  --rd-text: #111111;
  --rd-text2: #6E6E73;
  --rd-border: #E5E5EA;
  --rd-accent: #007AFF;
  --rd-success: #34C759;
  --rd-warning: #FF9500;
  --rd-danger: #FF3B30;
  --rd-radius: 16px;
  --rd-radius-sm: 12px;
  --rd-shadow: 0 1px 3px rgba(0,0,0,.04), 0 1px 2px rgba(0,0,0,.03);
  --rd-font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
             "Inter", "Helvetica Neue", Arial, sans-serif;
}

/* ---- Base canvas + typography ----------------------------------------- */
html, body, [class*="css"], .stApp, [data-testid="stMarkdownContainer"] {
  font-family: var(--rd-font);
  font-feature-settings: "kern" 1, "liga" 1;
  -webkit-font-smoothing: antialiased;
}
.stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] { background: var(--rd-bg); }
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1180px; }
[data-testid="stHeader"] { background: transparent; }
h1, h2, h3 { letter-spacing: -.012em; color: var(--rd-text); }

/* ---- Sidebar (clean white rail) --------------------------------------- */
[data-testid="stSidebar"] { background: var(--rd-card); border-right: 1px solid var(--rd-border); }
[data-testid="stSidebar"] .block-container { padding-top: 1.4rem; }
/* Primary section nav: a clean vertical list of large targets. */
[data-testid="stSidebar"] [role="radiogroup"] { gap: 2px; }
[data-testid="stSidebar"] [role="radiogroup"] label {
  border-radius: 10px; padding: 7px 10px; margin: 0; transition: background .12s ease;
}
[data-testid="stSidebar"] [role="radiogroup"] label:hover { background: rgba(0,122,255,.06); }

/* ---- Hero (a calm white card, not a loud gradient) -------------------- */
.rd-hero {
  border: 1px solid var(--rd-border);
  border-radius: var(--rd-radius);
  padding: 26px 30px;
  margin-bottom: 14px;
  background: var(--rd-card);
  box-shadow: var(--rd-shadow);
}
.rd-hero-eyebrow {
  font-size: .70rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
  color: var(--rd-accent); margin-bottom: 8px;
}
.rd-hero-title { font-size: 2.0rem; font-weight: 700; line-height: 1.12; margin: 0;
  letter-spacing: -.02em; color: var(--rd-text); }
.rd-hero-sub { font-size: 1.02rem; color: var(--rd-text2); margin-top: 8px; max-width: 74ch;
  line-height: 1.5; }
.rd-hero-chips { margin-top: 16px; display: flex; flex-wrap: wrap; gap: 8px; }

/* ---- Page header (per section) ---------------------------------------- */
.rd-page-eyebrow {
  font-size: .68rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
  color: var(--rd-accent);
}
.rd-page-title { font-size: 1.5rem; font-weight: 700; line-height: 1.18; margin: 2px 0 0 0;
  letter-spacing: -.015em; color: var(--rd-text); }
.rd-page-sub { font-size: .96rem; color: var(--rd-text2); margin: 5px 0 2px 0; max-width: 78ch;
  line-height: 1.5; }

/* ---- Section heading --------------------------------------------------- */
.rd-section { display: flex; align-items: baseline; gap: 10px; margin: 8px 0 4px 0; }
.rd-section-title { font-size: 1.02rem; font-weight: 650; color: var(--rd-text); }
.rd-section-sub { font-size: .84rem; color: var(--rd-text2); }

/* ---- Badges (status pills) -------------------------------------------- */
.rd-badge {
  display: inline-block; padding: 3px 11px; border-radius: 999px;
  font-size: .76rem; font-weight: 600; letter-spacing: .005em; white-space: nowrap;
  line-height: 1.5;
}

/* ---- Metric / status cards (HTML grid) -------------------------------- */
.rd-card-grid {
  display: grid; gap: 12px; margin: 8px 0 4px 0;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}
.rd-card {
  border: 1px solid var(--rd-border);
  border-radius: var(--rd-radius-sm);
  padding: 14px 16px;
  background: var(--rd-card);
  box-shadow: var(--rd-shadow);
}
.rd-card-accent { border-left: 4px solid var(--rd-border); }
.rd-card-label {
  font-size: .70rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: var(--rd-text2);
}
.rd-card-value { font-size: 1.5rem; font-weight: 700; line-height: 1.25; margin-top: 3px;
  color: var(--rd-text); }
.rd-card-caption { font-size: .76rem; color: var(--rd-text2); margin-top: 3px; }

/* ---- Engine roadmap cards --------------------------------------------- */
.rd-engine { border: 1px solid var(--rd-border); border-radius: var(--rd-radius-sm);
  padding: 14px 16px; background: var(--rd-card); box-shadow: var(--rd-shadow); height: 100%; }
.rd-engine-h { font-size: .72rem; font-weight: 700; letter-spacing: .03em; text-transform: uppercase;
  margin-bottom: 8px; display: flex; align-items: center; gap: 7px; }
.rd-dot { width: 9px; height: 9px; border-radius: 999px; display: inline-block; }
.rd-engine-row { font-size: .9rem; color: var(--rd-text); padding: 3px 0; line-height: 1.4; }
.rd-engine-sub { font-size: .78rem; color: var(--rd-text2); }

/* ---- Native st.metric -> card ----------------------------------------- */
[data-testid="stMetric"] {
  border: 1px solid var(--rd-border); border-radius: var(--rd-radius-sm);
  padding: 14px 16px; background: var(--rd-card); box-shadow: var(--rd-shadow);
}
[data-testid="stMetricLabel"] p {
  font-size: .70rem !important; font-weight: 600; letter-spacing: .03em;
  text-transform: uppercase; color: var(--rd-text2);
}
[data-testid="stMetricValue"] { font-size: 1.5rem; font-weight: 700; }

/* ---- Callout panels ---------------------------------------------------- */
.rd-panel {
  border: 1px solid var(--rd-c); border-left-width: 4px;
  border-radius: var(--rd-radius-sm); padding: 13px 16px; margin: 6px 0;
  background: var(--rd-bg-tint);
}
.rd-panel-title { font-weight: 650; margin-bottom: 2px; color: var(--rd-text); }
.rd-panel-body { font-size: .9rem; color: var(--rd-text); opacity: .92; }

/* ---- Workflow stepper -------------------------------------------------- */
.rd-steps { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 6px 0 8px 0; }
.rd-step {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 5px 12px; border-radius: 999px; font-size: .82rem; font-weight: 550;
  border: 1px solid var(--rd-border); background: var(--rd-card); color: var(--rd-text2);
}
.rd-step .rd-step-n {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 999px; font-size: .70rem; font-weight: 700;
  background: var(--rd-border); color: var(--rd-text);
}
.rd-step-done { border-color: rgba(52,199,89,.45); color: var(--rd-text); }
.rd-step-done .rd-step-n { background: var(--rd-success); color: #fff; }
.rd-step-current {
  border-color: rgba(0,122,255,.55); background: rgba(0,122,255,.07); color: var(--rd-text);
  font-weight: 650;
}
.rd-step-current .rd-step-n { background: var(--rd-accent); color: #fff; }
.rd-step-sep { color: var(--rd-text2); opacity: .5; font-size: .8rem; }

/* ---- Buttons (rounded, restrained) ------------------------------------ */
.stButton > button, .stDownloadButton > button {
  border-radius: 10px; border: 1px solid var(--rd-border); font-weight: 550;
  transition: all .12s ease;
}
.stButton > button:hover { border-color: var(--rd-accent); color: var(--rd-accent); }
.stButton > button[kind="primary"] {
  background: var(--rd-accent); border-color: var(--rd-accent); color: #fff;
}

/* ---- Chat bubbles ------------------------------------------------------ */
[data-testid="stChatMessage"] {
  background: var(--rd-card); border: 1px solid var(--rd-border);
  border-radius: 14px; box-shadow: var(--rd-shadow); padding: 4px 6px;
}
[data-testid="stChatInput"] textarea { border-radius: 12px; }

/* ---- Tabs (sub-navigation) -------------------------------------------- */
.stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid var(--rd-border); }
.stTabs [data-baseweb="tab"] { border-radius: 10px 10px 0 0; padding: 8px 16px; font-weight: 550; }
.stTabs [aria-selected="true"] { color: var(--rd-accent); }

/* ---- Expanders + bordered containers = white cards -------------------- */
[data-testid="stExpander"] {
  border: 1px solid var(--rd-border); border-radius: var(--rd-radius-sm);
  background: var(--rd-card); box-shadow: var(--rd-shadow);
}
[data-testid="stVerticalBlockBorderWrapper"] {
  border-radius: var(--rd-radius-sm); border-color: var(--rd-border) !important;
  background: var(--rd-card);
}
hr { opacity: .5; border-color: var(--rd-border); }
.rd-muted { color: var(--rd-text2); font-size: .85rem; }
</style>
"""


def inject_global_css() -> None:
    """Inject the global stylesheet.

    Must be called on **every** script run (do not guard it behind session
    state): Streamlit rebuilds its element tree each rerun and drops any element
    the current run did not re-emit, so a once-only ``<style>`` would vanish the
    first time the app reruns (e.g. when a run is selected). Re-emitting the same
    tag each run is idempotent — there is exactly one stylesheet element at this
    position per run. Uses ``st.html`` (renders no visible wrapper) when
    available, falling back to ``st.markdown``.
    """
    html_fn = getattr(st, "html", None)
    if callable(html_fn):
        html_fn(_GLOBAL_CSS)
    else:  # pragma: no cover - older Streamlit
        st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


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
    cls = "rd-card rd-card-accent" if status else "rd-card"
    border = f"border-left-color:{colour};" if status else ""
    return (f'<div class="{cls}" style="{border}">'
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


def render_engine_cards(status: dict) -> None:
    """Three engine-roadmap cards — Available now (green) / Planning support now (amber) /
    Future (neutral) — from ``domains.engine_status()``. Presentation only.
    """
    def _card(header: str, dot: str, rows: list[str]) -> str:
        body = "".join(f'<div class="rd-engine-row">{r}</div>' for r in rows)
        return (f'<div class="rd-engine"><div class="rd-engine-h">'
                f'<span class="rd-dot" style="background:{dot}"></span>{_html.escape(header)}</div>'
                f'{body}</div>')

    avail = [f'{_html.escape(e.get("capability", ""))} — '
             f'<b>{_html.escape(str(e.get("engine", "")).upper())}</b>'
             f'<div class="rd-engine-sub">{_html.escape(e.get("note", ""))}</div>'
             for e in status.get("available_now", [])]
    plan = [f'{_html.escape(e.get("capability", ""))}'
            f'<div class="rd-engine-sub">planning &amp; data templates — '
            f'{_html.escape(e.get("outcome", ""))}</div>'
            for e in status.get("planning_now", [])]
    future = [_html.escape(str(e)) for e in status.get("future", [])]
    cols = "".join([
        f'<div>{_card("Available now", "#34C759", avail)}</div>',
        f'<div>{_card("Planning support now", "#FF9500", plan)}</div>',
        f'<div>{_card("Future (modular)", "#8E8E93", future)}</div>',
    ])
    st.markdown('<div style="display:grid;gap:12px;'
                f'grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">{cols}</div>',
                unsafe_allow_html=True)


def render_section_card(title: str, body_md: str, *, status: str | None = None) -> None:
    """A simple bordered text card (title + markdown body) for short reference blurbs."""
    colour, tint = _style_for(status or "neutral")
    st.markdown(
        f'<div class="rd-panel" style="--rd-c:{colour};--rd-bg-tint:{tint}">'
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
        f'<div class="rd-panel" style="--rd-c:{colour};--rd-bg-tint:{tint}">'
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


def render_advanced_mode_note(tab_name: str) -> None:
    """A small 'Advanced controls' banner shown above each technical workflow.

    Frames the technical workflows as manual controls behind the Research Assistant, without
    changing any of their content. Presentation only.
    """
    st.caption(f"🔧 **Advanced controls** · {tab_name} — full manual controls for this step. "
               "Prefer the **Research Assistant** for a guided, conversational workflow.")
