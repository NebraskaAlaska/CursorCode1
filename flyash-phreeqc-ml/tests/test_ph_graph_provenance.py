"""pH-graph provenance + Simulate-tab plan-only guarantees (UI-level static checks).

These pin what the pH-graph audit established:
* the Simulate tab's orchestration function itself draws no graph and runs nothing directly
  (deterministic execution + result plots live in dedicated, gated helper functions), and
  it tells the user that changing plan values never updates the measured-vs-model graphs;
* every static pH PNG carries a source/timestamp provenance label;
* the live result figures are labelled "not affected by the Simulate tab";
* the measured-vs-model pH scatter is gated on inclusion's plotted rows
  (rows with a measured value AND a model prediction AND a valid mapping).

`app.py` is a Streamlit *script* (it calls st.set_page_config at import, so it can't be
imported as a module); we scan its source via the AST instead. The scientific result-path
boundary is covered separately by tests/test_ai_boundary.py and tests/test_inclusion.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

import flyash_phreeqc_ml as pkg

APP = Path(pkg.__file__).resolve().parent.parent / "app.py"


def _src() -> str:
    return APP.read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    src = _src()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            if seg:
                return seg
    raise AssertionError(f"{name} not found in app.py")


def test_simulate_tab_result_graphs_are_execution_gated_and_separate():
    s = _func_src("_render_simulate_tab")
    for forbidden in ("st.pyplot", "st.image", "savefig", "subprocess",
                      "phreeqc_runner", "build_input"):
        assert forbidden not in s, f"_render_simulate_tab must not reference {forbidden!r}"
    # The explicit user-facing wording: changing plan values never updates the result graphs.
    assert "changing simulation-plan values never updates" in s


def test_phreeqc_only_pH_figure_labelled_as_model_output():
    s = _func_src("_render_phreeqc_only_figures")
    assert "PHREEQC model output, not a measurement" in s
    assert "_png_provenance_caption" in s


def test_static_png_renderers_carry_provenance():
    for fn in ("_render_comparison_figures", "_render_phreeqc_only_figures"):
        assert "_png_provenance_caption" in _func_src(fn), fn


def test_png_provenance_helper_names_source_time_and_simulate():
    s = _func_src("_png_provenance_caption")
    assert "Source figure" in s and "generated" in s and "Simulate" in s


def test_live_result_figures_note_not_affected_by_simulate():
    src = _src()
    assert src.count("not affected by the Simulate tab") >= 2   # both live-note constants
    assert "st.caption(_LIVE_COMPARE_NOTE)" in src
    assert "st.caption(_LIVE_MEASURED_NOTE)" in src


def test_measured_vs_model_pH_scatter_gated_on_plotted_rows():
    src = _src()
    # the scatter only renders for inclusion's plotted rows (both measured + model present)
    assert 'if not inc["plotted"].empty:' in src
    assert 'comparison_scatter_figure(inc["plotted"]' in src
