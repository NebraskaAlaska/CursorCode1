"""Boundary pins for the UI modularization (app.py -> ui/ tab modules).

The Streamlit UI was split out of the monolithic ``app.py`` into a ``ui/`` package: shared
state/helpers (``ui/state.py``, ``ui/common.py``, ``ui/formatters.py``) plus one module per
tab (``ui/<tab>_tab.py``), each exposing ``render``. ``app.py`` stays a thin entry point (page
config + run-management sidebar + tab dispatch). These tests keep that structure honest:

* the **scientific** package never imports the UI (the dependency arrow points one way);
* the UI **base** modules (state/common/formatters) never import a tab module, and no tab
  module imports another tab module — so the ``ui/`` import graph stays an acyclic DAG;
* every ``ui/<tab>_tab.py`` imports cleanly (no import-time side effects) and exposes
  ``render``;
* ``app.py`` remains thin and dispatches to ``ui.<tab>.render``.

This complements ``tests/test_ai_boundary.py`` (AI/ML off the result path) and
``tests/test_app_tabs_smoke.py`` (the full app renders every tab via AppTest).
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import flyash_phreeqc_ml as pkg

_REPO = Path(pkg.__file__).resolve().parent.parent
_UI = _REPO / "ui"
_SCI = Path(pkg.__file__).resolve().parent          # flyash_phreeqc_ml/

# Render-exposing UI modules dispatched by app.py: the workflow modules + the section
# modules (results / engine_library / settings). The Assistant (assistant_tab) is the main
# workspace; Workspace = simulate_tab; Data & Validation = import/validate/match/compare;
# Projects = export_tab.
TAB_MODULES = ["assistant_tab", "simulate_tab", "import_tab", "validate_tab",
               "match_tab", "compare_tab", "export_tab", "results", "engine_library", "settings",
               "evidence_library"]
BASE_MODULES = ["state", "common", "formatters"]


def _import_targets(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            out.append(base)
            out += [f"{base}.{a.name}" for a in node.names]
    return out


# --------------------------------------------------------------------------- #
# The dependency arrow points one way: science never imports the UI.
# --------------------------------------------------------------------------- #
def test_scientific_package_does_not_import_ui_or_app():
    offenders = []
    for path in _SCI.rglob("*.py"):
        for t in _import_targets(path):
            top = t.lstrip(".").split(".")[0]
            if top in ("ui", "app"):
                offenders.append(f"{path.relative_to(_REPO)} -> {t}")
    assert not offenders, f"scientific modules import the UI: {offenders}"


# --------------------------------------------------------------------------- #
# The ui/ import graph is an acyclic DAG: base never imports tabs; tabs never
# import each other.
# --------------------------------------------------------------------------- #
def test_ui_base_modules_do_not_import_tab_modules():
    for base in BASE_MODULES:
        targets = _import_targets(_UI / f"{base}.py")
        offenders = [t for t in targets if any(f"{tab}" in t for tab in TAB_MODULES)]
        assert not offenders, f"ui/{base}.py imports a tab module: {offenders}"


def test_no_tab_module_imports_another_tab():
    for tab in TAB_MODULES:
        targets = _import_targets(_UI / f"{tab}.py")
        others = [o for o in TAB_MODULES if o != tab]
        offenders = [t for t in targets if any(o in t for o in others)]
        assert not offenders, f"ui/{tab}.py imports another tab module: {offenders}"


# --------------------------------------------------------------------------- #
# Every UI module imports cleanly (no import-time side effects) and each tab
# exposes render.
# --------------------------------------------------------------------------- #
def test_all_ui_modules_import_cleanly():
    for name in BASE_MODULES + TAB_MODULES:
        mod = importlib.import_module(f"ui.{name}")
        assert mod is not None


def test_every_tab_module_exposes_render():
    for tab in TAB_MODULES:
        mod = importlib.import_module(f"ui.{tab}")
        assert callable(getattr(mod, "render", None)), f"ui/{tab}.py has no callable render"


# --------------------------------------------------------------------------- #
# app.py is a thin entry point.
# --------------------------------------------------------------------------- #
def test_app_py_is_thin():
    src = (_REPO / "app.py").read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    assert len(code_lines) < 400, f"app.py has {len(code_lines)} code lines — keep it thin"
    # The only top-level function app.py keeps is the run-management sidebar; the AI settings
    # moved into the Engine Settings section, and section dispatch is inline.
    top_funcs = {n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
    assert top_funcs == {"_render_run_sidebar"}, top_funcs


def test_app_dispatches_to_ui_render():
    src = (_REPO / "app.py").read_text(encoding="utf-8")
    for tab in TAB_MODULES:
        assert f"{tab}.render(" in src, f"app.py does not dispatch to ui.{tab}.render"
