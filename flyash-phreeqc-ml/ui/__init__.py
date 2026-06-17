"""UI layer package — tab renderers + shared state/helpers (see docs/refactor_plan.md).

The Streamlit UI lives here: one module per tab (``ui/<tab>_tab.py``, each exposing
``render``) plus shared state (``ui/state.py``), shared render helpers (``ui/common.py``)
and pure formatters (``ui/formatters.py``). ``app.py`` is a thin entry point that wires the
run-management sidebar and dispatches to ``ui.<tab>.render(...)``. The scientific package
(``flyash_phreeqc_ml``) never imports this package — see ``tests/test_ui_modularization.py``.
"""
