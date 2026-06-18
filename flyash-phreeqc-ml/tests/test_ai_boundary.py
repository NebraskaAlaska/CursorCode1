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
# Note: simulation/source_terms.py is a materials-adjacent helper (it legitimately imports
# materials.profile_schema for atomic weights / oxide stoichiometry), so it is NOT in
# PLANNER_MODULES; its no-AI / no-executor / no-result-path boundary is pinned separately below.


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
        offenders = _mentions(_import_targets(mod), ("phreeqc_executor", "batch_executor"))
        assert not offenders, f"{mod} imports the executor: {offenders}"


# --------------------------------------------------------------------------- #
# Small-sweep batch executor boundary — orchestrates the executor, nothing else
# --------------------------------------------------------------------------- #
BATCH_MODULE = "simulation/batch_executor.py"


def test_batch_executor_imports_no_ai():
    targets = _import_targets(BATCH_MODULE)
    offenders = _mentions(targets, AI_MARKERS) + [
        t for t in targets if t in ("..ai", ".ai")
        or t.startswith("..ai.") or t.startswith(".ai.")]
    assert not offenders, f"{BATCH_MODULE} imports AI: {offenders}"


def test_batch_executor_imports_no_result_path():
    """It may import the single-scenario executor, but no comparison/residual/mapping code,
    no runner, and no run_manager."""
    forbidden = ("residuals", "inclusion", "mapping_table", "scenarios", "replicates",
                 "attribution", "mass_balance", "report", "surrogate", "residual_model",
                 "residual_stats", "incompleteness", "phreeqc_runner", "run_manager")
    offenders = _mentions(_import_targets(BATCH_MODULE), forbidden)
    assert not offenders, f"{BATCH_MODULE} imports result-path code: {offenders}"


# --------------------------------------------------------------------------- #
# Simulation run registry boundary — provenance store, off the result path
# --------------------------------------------------------------------------- #
REGISTRY_MODULE = "simulation/run_registry.py"


def test_run_registry_imports_no_ai():
    targets = _import_targets(REGISTRY_MODULE)
    offenders = _mentions(targets, AI_MARKERS) + [
        t for t in targets if t in ("..ai", ".ai")
        or t.startswith("..ai.") or t.startswith(".ai.")]
    assert not offenders, f"{REGISTRY_MODULE} imports AI: {offenders}"


def test_run_registry_imports_no_result_path():
    """The registry may import the executors (for data shapes + the safe-path guard), but no
    comparison/residual/mapping/validation code and not the Match-tab runner / run_manager."""
    forbidden = ("residuals", "inclusion", "mapping_table", "scenarios", "replicates",
                 "attribution", "mass_balance", "report", "surrogate", "residual_model",
                 "residual_stats", "incompleteness", "phreeqc_runner", "run_manager")
    offenders = _mentions(_import_targets(REGISTRY_MODULE), forbidden)
    assert not offenders, f"{REGISTRY_MODULE} imports result-path code: {offenders}"


def test_result_path_does_not_import_run_registry():
    for mod in RESULT_PATH_MODULES:
        offenders = _mentions(_import_targets(mod), ("run_registry",))
        assert not offenders, f"{mod} imports the run registry: {offenders}"


# --------------------------------------------------------------------------- #
# Simulation strategy / ranking boundary — pure optimisation over predictions
# --------------------------------------------------------------------------- #
STRATEGY_MODULE = "simulation/strategy.py"


def test_strategy_imports_no_ai():
    targets = _import_targets(STRATEGY_MODULE)
    offenders = _mentions(targets, AI_MARKERS) + [
        t for t in targets if t in ("..ai", ".ai")
        or t.startswith("..ai.") or t.startswith(".ai.")]
    assert not offenders, f"{STRATEGY_MODULE} imports AI: {offenders}"


def test_strategy_imports_no_result_path_or_executor():
    """Ranking is pure: it scores a table and imports no executor (cannot run anything), no
    comparison/residual/mapping code, and no AI."""
    forbidden = ("residuals", "inclusion", "mapping_table", "scenarios", "replicates",
                 "attribution", "mass_balance", "report", "surrogate", "residual_model",
                 "residual_stats", "incompleteness", "phreeqc_runner", "run_manager",
                 "phreeqc_executor", "batch_executor", "subprocess")
    offenders = _mentions(_import_targets(STRATEGY_MODULE), forbidden)
    assert not offenders, f"{STRATEGY_MODULE} imports execution/result-path code: {offenders}"


def test_result_path_does_not_import_strategy():
    for mod in RESULT_PATH_MODULES:
        offenders = _mentions(_import_targets(mod), ("simulation.strategy", ".strategy"))
        assert not offenders, f"{mod} imports the strategy module: {offenders}"


# --------------------------------------------------------------------------- #
# Material source-term / dissolution layer boundary — planning helper only
# --------------------------------------------------------------------------- #
SOURCE_TERMS_MODULE = "simulation/source_terms.py"


def test_source_terms_imports_no_ai_or_executor_or_result_path():
    """The dissolution/source-term layer templates text only: no AI (it cannot decide release
    fractions), no executor (it runs nothing), and no comparison/residual/mapping code."""
    forbidden = ("import_assist", "scenario_parser", "ai.literature", "ai.config", "ai.client",
                 ".assistant", ".literature", "phreeqc_executor", "batch_executor",
                 "phreeqc_runner", "subprocess", "residuals", "inclusion", "mapping_table",
                 "scenarios", "replicates", "attribution", "mass_balance", "run_manager")
    targets = _import_targets(SOURCE_TERMS_MODULE)
    offenders = _mentions(targets, forbidden) + [
        t for t in targets if t in ("..ai", ".ai") or t.startswith("..ai.") or t.startswith(".ai.")]
    assert not offenders, f"{SOURCE_TERMS_MODULE} imports forbidden module: {offenders}"


def test_result_path_does_not_import_source_terms():
    for mod in RESULT_PATH_MODULES:
        offenders = _mentions(_import_targets(mod), ("source_terms",))
        assert not offenders, f"{mod} imports source_terms: {offenders}"


# --------------------------------------------------------------------------- #
# Database-compatibility + phase-template layer boundary — pure helpers
# --------------------------------------------------------------------------- #
DB_PHASE_MODULES = ("simulation/database_compatibility.py", "simulation/phase_templates.py")


def test_db_compat_and_phase_modules_import_no_ai_executor_or_result_path():
    """They read database text + describe phase templates only: no AI (cannot control phase
    selection), no executor / subprocess (run nothing), no comparison/residual/mapping code."""
    forbidden = ("import_assist", "scenario_parser", "ai.literature", "ai.config", "ai.client",
                 ".assistant", ".literature", "phreeqc_executor", "batch_executor",
                 "phreeqc_runner", "subprocess", "residuals", "inclusion", "mapping_table",
                 "scenarios", "replicates", "attribution", "mass_balance", "run_manager")
    for mod in DB_PHASE_MODULES:
        targets = _import_targets(mod)
        offenders = _mentions(targets, forbidden) + [
            t for t in targets if t in ("..ai", ".ai")
            or t.startswith("..ai.") or t.startswith(".ai.")]
        assert not offenders, f"{mod} imports forbidden module: {offenders}"


def test_result_path_does_not_import_db_compat_or_phase_templates():
    for mod in RESULT_PATH_MODULES:
        offenders = _mentions(_import_targets(mod),
                              ("database_compatibility", "phase_templates"))
        assert not offenders, f"{mod} imports db-compat/phase-template code: {offenders}"


# --------------------------------------------------------------------------- #
# Target-matching (inverse search) boundary — a pure target-parse + scoring helper
# --------------------------------------------------------------------------- #
TARGET_MATCHING_MODULE = "simulation/target_matching.py"


def test_target_matching_imports_no_ai_executor_or_result_path():
    """Inverse search parses targets + scores an executed result table only: no AI (it cannot
    invent a target value or a release fraction), no executor / subprocess (it runs nothing
    itself — execution happens via the batch executor on an explicit UI click), and no
    comparison/residual/mapping code (it is off the scientific result path)."""
    forbidden = ("import_assist", "scenario_parser", "ai.literature", "ai.config", "ai.client",
                 ".assistant", ".literature", "phreeqc_executor", "batch_executor",
                 "phreeqc_runner", "phreeqc_input_builder", "subprocess", "residuals",
                 "inclusion", "mapping_table", "scenarios", "replicates", "attribution",
                 "mass_balance", "run_manager", "run_registry")
    targets = _import_targets(TARGET_MATCHING_MODULE)
    offenders = _mentions(targets, forbidden) + [
        t for t in targets if t in ("..ai", ".ai") or t.startswith("..ai.") or t.startswith(".ai.")]
    assert not offenders, f"{TARGET_MATCHING_MODULE} imports forbidden module: {offenders}"


def test_result_path_does_not_import_target_matching():
    """The result path never imports the inverse-search layer (a match is not validation)."""
    for mod in RESULT_PATH_MODULES:
        offenders = _mentions(_import_targets(mod), ("target_matching",))
        assert not offenders, f"{mod} imports target_matching: {offenders}"


# --------------------------------------------------------------------------- #
# AI agent orchestration layer boundary — the LLM proposes; deterministic code runs
# --------------------------------------------------------------------------- #
# The agent's PURE modules (decision/data) must reach no AI and no execution code, so "can
# this run?" never depends on importing PHREEQC. The AI-touching agent modules are the
# orchestrator AND the NLU extractor (the latter mirrors ai/scenario_parser — AI-first extraction
# with a deterministic fallback); both still import no executor and no result path. The tool
# registry may import the executor/builders but NO AI (AI never writes input, runs nothing, or
# decides a composition/release fraction).
AGENT_PURE_MODULES = [
    "agent/agent_state.py", "agent/agent_actions.py", "agent/agent_prompts.py",
    "agent/agent_policy.py", "agent/domains.py",
]
AGENT_TOOL_MODULE = "agent/tool_registry.py"
AGENT_NLU_MODULE = "agent/nlu_extractor.py"
AGENT_COUNCIL_MODULE = "agent/agent_council.py"
_EXECUTOR_MARKERS = ("phreeqc_executor", "batch_executor", "phreeqc_runner", "subprocess")


def _imports_ai(targets) -> list:
    return _mentions(targets, AI_MARKERS) + [
        t for t in targets if t in ("..ai", ".ai")
        or t.startswith("..ai.") or t.startswith(".ai.")]


def test_agent_pure_modules_import_no_ai_or_executor():
    """State / actions / prompts / policy / domains are pure decision+data: no AI helper and no
    PHREEQC executor/subprocess. (The merge legitimately reuses the rule parser + safety.)"""
    for mod in AGENT_PURE_MODULES:
        targets = _import_targets(mod)
        offenders = _imports_ai(targets) + _mentions(targets, _EXECUTOR_MARKERS)
        assert not offenders, f"{mod} imports AI/executor: {offenders}"


def test_agent_tool_registry_imports_no_ai():
    """The 'doing' layer may import the executor/builders, but NEVER an AI helper — AI does not
    write PHREEQC input, run anything, or decide a composition/release fraction."""
    offenders = _imports_ai(_import_targets(AGENT_TOOL_MODULE))
    assert not offenders, f"{AGENT_TOOL_MODULE} imports AI: {offenders}"


def test_nlu_extractor_imports_no_executor():
    """The NLU layer is AI-first (it may import the AI client, like ai/scenario_parser), but it
    runs NOTHING — no executor / runner / subprocess ever appears in it."""
    offenders = _mentions(_import_targets(AGENT_NLU_MODULE), _EXECUTOR_MARKERS)
    assert not offenders, f"{AGENT_NLU_MODULE} imports an executor: {offenders}"


def test_agent_council_imports_no_executor_or_tool_registry():
    """The council is ADVISORY: it may import the AI client (like the orchestrator / NLU), but it
    runs nothing and chooses no action — it imports no executor and no tool registry."""
    targets = _import_targets(AGENT_COUNCIL_MODULE)
    offenders = _mentions(targets, _EXECUTOR_MARKERS + ("tool_registry",))
    assert not offenders, f"{AGENT_COUNCIL_MODULE} imports executor/tool_registry: {offenders}"


def test_agent_modules_do_not_import_result_path():
    """No agent module imports the comparison/residual/mapping/validation code or the Match-tab
    runner — the agent is off the scientific result path (it orchestrates the Simulate side)."""
    forbidden = ("residuals", "inclusion", "mapping_table", "scenarios", "replicates",
                 "attribution", "mass_balance", "report", "surrogate", "residual_model",
                 "residual_stats", "incompleteness", "phreeqc_runner", "run_manager")
    for mod in AGENT_PURE_MODULES + [AGENT_TOOL_MODULE, AGENT_NLU_MODULE, AGENT_COUNCIL_MODULE,
                                     "agent/agent_orchestrator.py", "agent/__init__.py"]:
        offenders = _mentions(_import_targets(mod), forbidden)
        assert not offenders, f"{mod} imports result-path code: {offenders}"


def test_result_path_does_not_import_agent():
    """The scientific result path never imports the agent layer (the reverse direction)."""
    for mod in RESULT_PATH_MODULES:
        targets = _import_targets(mod)
        offenders = _mentions(targets, ("agent_state", "agent_orchestrator", "agent_policy",
                                        "tool_registry", "agent_actions")) + [
            t for t in targets if t in ("..agent", ".agent")
            or t.startswith("..agent.") or t.startswith(".agent.")]
        assert not offenders, f"{mod} imports the agent layer: {offenders}"


def test_planner_and_executor_do_not_import_agent():
    """The planner + execution layers don't depend on the agent (the agent depends on them)."""
    for mod in PLANNER_MODULES + [EXECUTOR_MODULE, BATCH_MODULE, REGISTRY_MODULE,
                                  STRATEGY_MODULE, TARGET_MATCHING_MODULE, SOURCE_TERMS_MODULE]:
        targets = _import_targets(mod)
        offenders = [t for t in targets if t in ("..agent", ".agent")
                     or t.startswith("..agent.") or t.startswith(".agent.")]
        assert not offenders, f"{mod} imports the agent layer: {offenders}"


# --------------------------------------------------------------------------- #
# Literature research + evidence library boundary — off the scientific result path
# --------------------------------------------------------------------------- #
# Only `extraction` may import the AI client; nothing here imports an executor or the result path,
# and the result path never imports the literature package — extracted evidence is a *future*
# training library, never a measured/validated value.
LITERATURE_NON_AI_MODULES = [
    "literature/source_schema.py", "literature/evidence_schema.py", "literature/search_clients.py",
    "literature/ranking.py", "literature/evidence_store.py", "literature/research_agent.py",
]
LITERATURE_AI_MODULE = "literature/extraction.py"
_RESULT_PATH_MARKERS = ("residuals", "inclusion", "mapping_table", "scenarios", "replicates",
                        "attribution", "mass_balance", "report", "surrogate", "residual_model",
                        "residual_stats", "incompleteness", "phreeqc_runner", "run_manager")


def test_literature_non_ai_modules_import_no_ai():
    """The schemas / clients / ranking / store / research agent reach NO AI helper — only the
    extraction module may (it mirrors ai/scenario_parser)."""
    for mod in LITERATURE_NON_AI_MODULES:
        offenders = _imports_ai(_import_targets(mod))
        assert not offenders, f"{mod} imports AI: {offenders}"


def test_literature_modules_import_no_executor_or_result_path():
    """No literature module imports an executor or the comparison/residual/mapping result path
    (the AI extraction module may import the AI client, but runs nothing)."""
    for mod in LITERATURE_NON_AI_MODULES + [LITERATURE_AI_MODULE]:
        targets = _import_targets(mod)
        offenders = _mentions(targets, _EXECUTOR_MARKERS + _RESULT_PATH_MARKERS)
        assert not offenders, f"{mod} imports executor/result-path: {offenders}"


def test_result_path_does_not_import_literature():
    """The scientific result path never imports the literature package (the reverse direction)."""
    for mod in RESULT_PATH_MODULES:
        targets = _import_targets(mod)
        offenders = [t for t in targets if t in ("..literature", ".literature")
                     or t.startswith("..literature.") or t.startswith(".literature.")]
        assert not offenders, f"{mod} imports the literature package: {offenders}"
