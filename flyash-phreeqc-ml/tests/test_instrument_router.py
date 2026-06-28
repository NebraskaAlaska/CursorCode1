"""Pins for the **instrument router** — deterministic prompt → instrument mapping, advisory only.

The router maps the manual-verification prompts to the right instrument(s) and, critically:

* a **mechanical / strength** prompt must NOT route to PHREEQC,
* a **leaching** prompt still routes to PHREEQC,
* **ICP** and **XRD** prompts route to their modules (XRD/ICP cues beat domain classification),
* and the router **never auto-runs** anything (``auto_run`` is always False; it owns no executor).
"""
from __future__ import annotations

import inspect
import re

from flyash_phreeqc_ml.instruments import instrument_registry as reg
from flyash_phreeqc_ml.instruments import instrument_router as router


def _route(prompt, **kw):
    return router.route(prompt, **kw)


# --------------------------------------------------------------------------- #
# The six manual-verification prompts route as specified.
# --------------------------------------------------------------------------- #
def test_convert_mgL_routes_to_icp():
    rr = _route("convert ICP Ca 84 mg/L and Si 28 mg/L to mM with dilution factor 10")
    assert rr.primary == reg.ICP_DATA_PROCESSOR
    assert reg.PHREEQC_LEACHING not in rr.instruments     # a plain conversion, no comparison


def test_measured_vs_phreeqc_routes_to_icp_with_validation():
    rr = _route("I measured ICP Ca 2.1 mM and Si 0.8 mM, compare with PHREEQC predicted "
                "Ca 2.5 mM and Si 0.7 mM")
    assert rr.primary == reg.ICP_DATA_PROCESSOR
    assert rr.validation is True
    assert reg.PHREEQC_LEACHING in rr.instruments         # PHREEQC supplies the predictions
    assert rr.validation_options                          # comparison options surfaced


def test_xrd_peaks_prompt_routes_to_xrd():
    rr = _route("simulate expected XRD peaks for calcite quartz and portlandite")
    assert rr.primary == reg.XRD_ADVISORY


def test_xrd_phases_after_leaching_routes_to_xrd_with_phreeqc_context():
    rr = _route("what XRD phases should I check after NaOH leaching of Class C fly ash?")
    assert rr.primary == reg.XRD_ADVISORY                  # XRD intent beats the leaching domain
    assert reg.PHREEQC_LEACHING in rr.instruments          # leaching context added


def test_leaching_prompt_routes_to_phreeqc():
    rr = _route("im leeching class c fli ash w naoh .5m 2g 10ml for 1hr room temp wanna ph ca si")
    assert rr.primary == reg.PHREEQC_LEACHING
    # The bare molarity ".5m" must NOT be misread as an ICP measurement.
    assert rr.primary != reg.ICP_DATA_PROCESSOR


def test_strength_prompt_does_not_route_to_phreeqc():
    rr = _route("mixing fly ash with PET plastic, predict 28 day compressive strength")
    assert reg.PHREEQC_LEACHING not in rr.instruments
    assert rr.primary in (reg.ML_SURROGATE_PREDICTOR, reg.MECHANICAL_TEST_PROCESSOR)


def test_estimate_ph_leaching_routes_to_phreeqc():
    rr = _route("leach fly ash with NaOH and estimate pH")
    assert rr.primary == reg.PHREEQC_LEACHING


# --------------------------------------------------------------------------- #
# Safety: the router is advisory — it never runs anything.
# --------------------------------------------------------------------------- #
def test_router_never_auto_runs():
    for prompt in ("leach fly ash with NaOH and estimate pH",
                   "convert Ca 84 mg/L to mM",
                   "expected XRD peaks for calcite"):
        assert _route(prompt).auto_run is False


def test_router_module_owns_no_executor():
    """The router imports no executor / run path — it cannot execute even if asked."""
    src = inspect.getsource(router)
    assert "phreeqc_executor" not in src and "batch_executor" not in src
    assert "tool_registry" not in src
    # No public 'run'/'execute' entry point on the router.
    assert not hasattr(router, "execute")
    assert not hasattr(router, "run_simulation")


def test_router_imports_no_ai():
    src = inspect.getsource(router)
    assert "import openai" not in src and "anthropic" not in src.lower()


# --------------------------------------------------------------------------- #
# Mode flags flow into the result (design/state support).
# --------------------------------------------------------------------------- #
def test_uncertainty_mode_surfaces_sensitivity_variables():
    rr = _route("leach fly ash with NaOH and estimate pH", uncertainty_mode=True)
    assert rr.uncertainty_options
    assert any("release fraction" in o for o in rr.uncertainty_options)


def test_validation_mode_adds_validation_options_without_measured_data():
    rr = _route("leach fly ash with NaOH and estimate pH", validation_mode=True)
    assert rr.validation is True
    # ...but it never asserts the run is validated (only options to make it comparable).
    assert all("validated" not in o or "never labels" in o for o in rr.validation_options)


def test_to_card_is_renderable_and_marks_no_autorun():
    card = _route("convert Ca 84 mg/L to mM").to_card()
    assert card["primary"] == reg.ICP_DATA_PROCESSOR
    assert card["auto_run"] is False
    assert isinstance(card["recommended"], list) and card["recommended"]


_SECRET_RE = re.compile(r"sk-[A-Za-z0-9]{8,}|api[_-]?key|secret", re.I)


def test_routing_text_carries_no_secret():
    rr = _route("I measured ICP Ca 2.1 mM, compare with PHREEQC")
    blob = " ".join([rr.objective, rr.next_action, rr.rationale, *rr.warnings])
    assert not _SECRET_RE.search(blob)
