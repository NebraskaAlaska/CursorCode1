"""Boundary pins: AI/ML stay off the scientific result path.

The AI configuration/client work must not let AI (or trained ML) reach the code that
computes mapping status, residuals, validation status, comparison inclusion, or the
comparison data. This is enforced by inspecting each module's imports with the AST (robust
to comments/strings): the result-path modules import no AI helper and no trained-ML model,
and — the reverse — the AI config/client layers import no science module.

This complements the runtime guarantees: the AI helpers are suggestion/interpretation only
and the trained-ML overlays are display-only and hard-gated.
"""
from __future__ import annotations

import ast
from pathlib import Path

import flyash_phreeqc_ml as pkg

PKG_DIR = Path(pkg.__file__).resolve().parent


def _import_targets(rel_path: str) -> list[str]:
    """All imported module/name targets in a file (module-level *and* nested), via AST."""
    tree = ast.parse((PKG_DIR / rel_path).read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            out.append(base)
            out += [f"{base}.{a.name}" for a in node.names]
    return out


def _mentions(targets, markers) -> list[str]:
    return sorted({t for t in targets for m in markers if m in t})


# Modules that compute the scientific result path. None may import AI or trained ML.
RESULT_PATH_MODULES = [
    "compare/residuals.py",
    "compare/inclusion.py",
    "scenarios.py",
    "replicates.py",
    "mapping_table.py",
    "attribution.py",
    "mass_balance.py",
]

AI_MARKERS = ("import_assist", "ai.assistant", "ai.literature", "ai.config", "ai.client",
              ".assistant", ".literature")
TRAINED_ML_MARKERS = ("surrogate", "residual_model", "incompleteness_model")


def test_result_path_modules_do_not_import_ai():
    for mod in RESULT_PATH_MODULES:
        targets = _import_targets(mod)
        offenders = _mentions(targets, AI_MARKERS) + [
            t for t in targets
            if t in ("..ai", ".ai") or t.startswith("..ai.") or t.startswith(".ai.")]
        assert not offenders, f"{mod} imports AI: {offenders}"


def test_result_path_modules_do_not_import_trained_ml():
    for mod in RESULT_PATH_MODULES:
        offenders = _mentions(_import_targets(mod), TRAINED_ML_MARKERS)
        assert not offenders, f"{mod} imports trained ML: {offenders}"


# The AI config/client layers must not reach any science module (the reverse direction).
SCIENCE_MARKERS = ("residuals", "inclusion", "scenarios", "replicates", "mapping_table",
                   "attribution", "mass_balance", "report", "surrogate", "residual_model",
                   "residual_stats", "incompleteness")


def test_ai_config_layer_imports_no_science():
    for mod in ("ai/config.py", "ai/client.py"):
        offenders = _mentions(_import_targets(mod), SCIENCE_MARKERS)
        assert not offenders, f"{mod} imports science: {offenders}"


def test_ai_config_keeps_streamlit_and_anthropic_lazy():
    """They must be imported inside functions (not module-level) so importing the package
    is cheap and safe with neither installed/running."""
    tree = ast.parse((PKG_DIR / "ai/config.py").read_text(encoding="utf-8"))
    module_level: list[str] = []
    for node in tree.body:                      # top-level statements only
        if isinstance(node, ast.Import):
            module_level += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            module_level.append(node.module or "")
    assert not [m for m in module_level if "streamlit" in m or "anthropic" in m]


def test_runtime_override_does_not_touch_inclusion_module():
    """Setting an AI model/provider override is inert for the science: the inclusion module
    exposes no AI hook to be influenced."""
    from flyash_phreeqc_ml.ai import config as ai_config
    from flyash_phreeqc_ml.compare import inclusion

    ai_config.clear_runtime_overrides()
    ai_config.set_runtime_overrides(provider="anthropic", model="some-other-model")
    try:
        assert not hasattr(inclusion, "resolve_model")
        assert not hasattr(inclusion, "is_enabled")
    finally:
        ai_config.clear_runtime_overrides()


# --------------------------------------------------------------------------- #
# Simulation planner boundary — a planning layer only (no PHREEQC, off the result path)
# --------------------------------------------------------------------------- #
PLANNER_MODULES = [
    "simulation/scenario_schema.py",
    "simulation/safety.py",
    "simulation/rule_parser.py",
    "simulation/matrix.py",
    "simulation/phreeqc_input_builder.py",
    "ai/scenario_parser.py",
]


def test_planner_does_not_execute_phreeqc():
    """The NL planner must never run PHREEQC — no planner module imports the runner."""
    for mod in PLANNER_MODULES:
        offenders = _mentions(_import_targets(mod), ("phreeqc_runner",))
        assert not offenders, f"{mod} imports phreeqc_runner: {offenders}"


def test_planner_does_not_import_result_path():
    """The planner must not import the comparison/residual/mapping/validation code."""
    forbidden = ("residuals", "inclusion", "mapping_table", "residual_model",
                 "incompleteness", "surrogate")
    for mod in PLANNER_MODULES:
        offenders = _mentions(_import_targets(mod), forbidden)
        assert not offenders, f"{mod} imports result-path code: {offenders}"


def test_result_path_does_not_import_planner():
    """The result path must not import the planner (it is off the scientific path)."""
    markers = ("scenario_parser", "scenario_schema", "phreeqc_input_builder")
    for mod in RESULT_PATH_MODULES:
        targets = _import_targets(mod)
        offenders = _mentions(targets, markers) + [
            t for t in targets if t in ("..simulation", ".simulation")
            or t.startswith("..simulation.") or t.startswith(".simulation.")]
        assert not offenders, f"{mod} imports the planner: {offenders}"


# --------------------------------------------------------------------------- #
# Material composition manager boundary — a planning-layer helper only
# --------------------------------------------------------------------------- #
def _imports_materials(targets) -> list[str]:
    return [t for t in targets
            if t in (".materials", "..materials")
            or t.endswith(".materials") or ".materials." in t
            or "materials.profile" in t]


def test_result_path_and_planner_do_not_import_materials_manager():
    """The material composition manager is a Simulate-only helper: no result-path module and
    no planner module imports it (the input-preview builder duck-types instead)."""
    for mod in RESULT_PATH_MODULES + PLANNER_MODULES:
        offenders = _imports_materials(_import_targets(mod))
        assert not offenders, f"{mod} imports the material profile manager: {offenders}"


def test_materials_manager_imports_no_science_or_planner():
    """The reverse direction: the manager reaches no science/result-path/planner module
    (it depends only on units for molar masses)."""
    forbidden = ("residuals", "inclusion", "scenarios", "replicates", "mapping_table",
                 "attribution", "mass_balance", "report", "surrogate", "residual_model",
                 "incompleteness", "scenario_parser", "phreeqc_input_builder", "phreeqc_runner")
    for mod in ("materials/profile_schema.py", "materials/profile_validation.py",
                "materials/__init__.py"):
        offenders = _mentions(_import_targets(mod), forbidden)
        assert not offenders, f"{mod} imports forbidden module: {offenders}"


# --------------------------------------------------------------------------- #
# PHREEQC execution layer boundary — runs PHREEQC, but stays off the result path
# --------------------------------------------------------------------------- #
EXECUTOR_MODULE = "simulation/phreeqc_executor.py"


def test_executor_imports_no_ai():
    """The execution layer must not import any AI helper (AI never writes/runs input)."""
    targets = _import_targets(EXECUTOR_MODULE)
    offenders = _mentions(targets, AI_MARKERS) + [
        t for t in targets if t in ("..ai", ".ai")
        or t.startswith("..ai.") or t.startswith(".ai.")]
    assert not offenders, f"{EXECUTOR_MODULE} imports AI: {offenders}"


def test_executor_imports_no_result_path():
    """The execution layer must not import the comparison/residual/mapping/validation code,
    nor the Match-tab runner (which appends to the shared results CSV). It depends only on
    config + the parsers."""
    forbidden = ("residuals", "inclusion", "mapping_table", "scenarios", "replicates",
                 "attribution", "mass_balance", "report", "surrogate", "residual_model",
                 "residual_stats", "incompleteness", "phreeqc_runner", "run_manager")
    offenders = _mentions(_import_targets(EXECUTOR_MODULE), forbidden)
    assert not offenders, f"{EXECUTOR_MODULE} imports result-path code: {offenders}"


def test_result_path_does_not_import_executor():
    """The result path must not import the executor (it is off the scientific path)."""
    for mod in RESULT_PATH_MODULES:
        offenders = _mentions(_import_targets(mod), ("phreeqc_executor",))
        assert not offenders, f"{mod} imports the executor: {offenders}"
