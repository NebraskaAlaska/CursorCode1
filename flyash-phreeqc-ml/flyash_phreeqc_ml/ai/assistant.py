"""Grounded "Ask the assistant" layer — answers about the selected run via tool use.

This is an **interpretation layer**, never a workflow step. It lets a user ask
natural-language questions about the currently-selected run, and the model answers
**only** by calling read-only tools that wrap the app's own functions — so every
number in an answer comes from the same code paths the UI uses, never from the
model's imagination.

Design rules (all enforced here):

* **Read-only tools.** Every tool wraps an existing pure/IO-read function
  (suggestion table, inclusion counts, bias table, provenance, mapping traces, …).
  None mutates a run, a mapping, a file, or any global state.
* **Grounded answers.** The system prompt forbids invented numbers, requires the
  model to cite which tool each figure came from, and forbids calling the model
  "validated" unless the inclusion tool reports ``validity == "valid"``.
* **Auditable.** :func:`answer` returns the final text *and* a ``trace`` of every
  tool call + a short summary of what it returned, so the UI can show a collapsed
  "data used" panel under each answer.
* **Disabled by default.** Like the import-assist, the assistant needs
  ``ANTHROPIC_API_KEY`` + the ``anthropic`` SDK; without them :func:`answer`
  returns a clean disabled result (no exception, no partial output).
* **Session-only history.** Conversation history is passed in by the caller and is
  never written to a run's files — the assistant has no persistence of its own.

The Anthropic SDK is only touched through the shared lazy client resolver in
:mod:`import_assist`, so importing this module never requires the SDK.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import config, mapping_table, profiles, replicates, run_manager, scenarios
from ..compare import inclusion
from ..ml import residual_stats
from . import import_assist
from .import_assist import _message_text, _model, _resolve_client, is_enabled  # reuse

# Re-exported so the UI checks one symbol.
__all__ = ["is_enabled", "answer", "RunContext", "AssistantAnswer", "TOOL_SPECS",
           "SYSTEM_PROMPT", "ASSISTANT_DATA_NOTICE", "ASSISTANT_CONSENT_LABEL"]

# Bounds — keep payloads small + the loop finite.
MAX_TOKENS = 1600
MAX_TOOL_ITERS = 8
MAX_ROWS = 100            # hard cap on get_comparison_rows
DEFAULT_ROWS = 25
MAX_LIST = 80            # cap on condition/mapping lists returned to the model

# Per-session consent (same spirit as Prompt 10's import-assist).
ASSISTANT_DATA_NOTICE = (
    "The assistant answers by sending your question plus **numeric summaries of the "
    "selected run** (mapping statuses, inclusion counts, the bias table, provenance, "
    "and mapping traces) to the Anthropic API — data leaves this machine for this "
    "feature only. It is read-only: it never changes your run, mappings, or files, "
    "and the conversation is kept in this session only, never saved to run files."
)
ASSISTANT_CONSENT_LABEL = (
    "I understand and allow sending my question + run summaries to the API for grounded answers."
)


# --------------------------------------------------------------------------- #
# System prompt (the guardrails the model must follow)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
You are a careful research assistant embedded in a fly-ash / PHREEQC geochemistry app.
You answer questions about ONE selected experiment run. You have read-only tools that
call the app's own functions; their results are the ONLY facts you may use.

Absolute rules:
1. Use ONLY numbers and statuses returned by the tools. NEVER invent, estimate, round
   away, or infer a number that a tool did not return. If the tools do not contain the
   answer, say so plainly and name what is missing.
2. Cite your sources. For every figure or status you state, name the tool it came from
   (e.g. "per get_inclusion_counts" or "from get_bias_table"). Keep citations inline.
3. Validation language is restricted. Do NOT describe the model/comparison as
   "validated" or "valid" unless get_inclusion_counts returns validity == "valid" for
   the variable in question. If validity is "preliminary", "single-sample", "unsafe",
   "needs new simulations", or "nothing to compare", say the comparison is a workflow
   check, not validation — and state which it is.
4. Be honest about insufficiency. If data is too sparse (e.g. get_bias_table marks rows
   sufficient=false, or there are too few exact pairs/conditions), say the estimate is
   not reliable yet and point to the gate counts the tools report (n_exact_pairs vs the
   threshold; number of conditions). Never paper over small n.
5. "What should I test next?" is answered as SUGGESTIONS, reasoned from get_unsafe_mappings,
   get_needed_simulations, and get_bias_table (which conditions are unsafe, which lack a
   model result, where bias is large or uncertain). Frame them as options to consider,
   not instructions, and tie each to the tool evidence.
6. You cannot change anything. You have no tools that write, map, train, or run — say so
   if asked to perform an action, and describe what the user would do in the app instead.

Style: concise, plain, and specific. Prefer a short direct answer followed by the
tool-cited evidence. When a tool returns an "error" field, treat that data as unavailable.
"""


# --------------------------------------------------------------------------- #
# Run context (frames the tools read; built once per question)
# --------------------------------------------------------------------------- #
@dataclass
class RunContext:
    """The read-only frames a run's tools operate on (no Streamlit, no mutation)."""

    run_name: str
    data: pd.DataFrame
    sample_mapping: pd.DataFrame
    condition_mapping: pd.DataFrame
    manifest: pd.DataFrame
    comparison_df: pd.DataFrame
    profile: object = field(default_factory=lambda: profiles.FLY_ASH_PROFILE)

    @classmethod
    def from_run(cls, run_name: str, profile=None) -> "RunContext":
        """Load a run's frames via :mod:`run_manager` + the global PHREEQC manifest.

        Every read is guarded: a non-lab run (no mapping/comparison) or a missing file
        degrades to an empty frame rather than raising, so the assistant can still
        answer "there is no comparison yet" honestly.
        """
        profile = profile or profiles.FLY_ASH_PROFILE
        data = _safe(lambda: run_manager.read_data_file(run_name), pd.DataFrame())
        sample_mapping = _safe(lambda: run_manager.read_mapping(run_name),
                               pd.DataFrame(columns=run_manager.MAPPING_COLUMNS))
        condition_mapping = _safe(lambda: run_manager.read_condition_mapping(run_name),
                                  pd.DataFrame(columns=run_manager.CONDITION_MAPPING_COLUMNS))
        comparison_df = _safe(lambda: _read_comparison(run_name), pd.DataFrame())
        return cls(run_name=run_name, data=data, sample_mapping=sample_mapping,
                   condition_mapping=condition_mapping, manifest=_load_manifest(),
                   comparison_df=comparison_df, profile=profile)


def _safe(fn, default):
    try:
        out = fn()
        return out if out is not None else default
    except Exception:
        return default


def _read_comparison(run_name: str) -> pd.DataFrame:
    path = run_manager.comparison_path(run_name)
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _load_manifest() -> pd.DataFrame:
    rp = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if rp.exists():
        try:
            return scenarios.build_scenario_manifest(pd.read_csv(rp))
        except Exception:
            pass
    return pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS)


# --------------------------------------------------------------------------- #
# JSON sanitising (numpy/NaN -> plain JSON the model + tests can read)
# --------------------------------------------------------------------------- #
def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.floating):
        obj = float(obj)
    elif isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, float):
        return None if math.isnan(obj) else obj
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    return str(obj)


def _records(df: pd.DataFrame, cols=None, limit=None) -> list[dict]:
    if df is None or df.empty:
        return []
    use = [c for c in cols if c in df.columns] if cols else list(df.columns)
    out = df[use]
    if limit is not None:
        out = out.head(limit)
    return _jsonable(out.to_dict("records"))


# --------------------------------------------------------------------------- #
# Tool implementations (read-only). Each returns a JSON-able dict with a `source`.
# --------------------------------------------------------------------------- #
def get_mapping_overview(ctx: RunContext) -> dict:
    """Suggestion table + overall mapping status for the run (condition-level)."""
    table = mapping_table.build_suggestion_table(
        ctx.data, ctx.manifest, ctx.condition_mapping, ctx.profile)
    overall = replicates.overall_mapping_status(ctx.data, ctx.sample_mapping, ctx.manifest)
    return {
        "source": "get_mapping_overview",
        "status_counts": _jsonable(overall["counts"]),
        "n_mapped": int(overall["n_mapped"]),
        "n_unmapped": int(overall["n_unmapped"]),
        "all_exact": bool(overall["all_exact"]),
        "overall_status": overall["overall"],
        "n_conditions": int(len(table)),
        "conditions": _records(table, [
            "condition_key", "n_replicates", "scenario_label", "mapping_status",
            "score", "confidence", "already_mapped"], limit=MAX_LIST),
    }


def get_inclusion_counts(ctx: RunContext, variable: str | None = None) -> dict:
    """Prompt-4 inclusion counts + validity per comparison variable (capped)."""
    spec = ctx.profile.comparison_variable_spec
    if variable and variable in spec:
        variables = [variable]
    else:
        variables = [v for v, (mcol, _p) in spec.items()
                     if mcol in ctx.comparison_df.columns]
    per = []
    for v in variables[:12]:
        inc = inclusion.comparison_inclusion(
            ctx.data, ctx.sample_mapping, ctx.comparison_df, v,
            manifest=ctx.manifest, profile=ctx.profile)
        per.append({
            "variable": v,
            "rows_plotted": inc["rows_plotted"],
            "rows_excluded": inc["n_total"] - inc["rows_plotted"],
            "unique_predictions_used": inc["unique_predictions_used"],
            "validity": inc["validity"],
            "validity_message": inc["validity_message"],
            "collapse_warning": inc["collapse_warning"],
            "status_counts": _jsonable(inc["status_counts"]),
        })
    return {
        "source": "get_inclusion_counts",
        "variables": per,
        "any_variable_valid": any(p["validity"] == inclusion.VALIDITY_VALID for p in per),
        "note": "Only validity == 'valid' means the model was validated for that variable.",
    }


def get_unsafe_mappings(ctx: RunContext) -> dict:
    """Saved condition mappings classified *unsafe* (known metadata conflicts)."""
    needed = replicates.conditions_needing_simulation(
        ctx.data, ctx.condition_mapping, ctx.manifest)
    unsafe = needed[needed["reason_needed"].astype(str).str.startswith("unsafe")] \
        if not needed.empty else needed
    return {
        "source": "get_unsafe_mappings",
        "n_unsafe": int(len(unsafe)),
        "unsafe": _records(unsafe, [
            "condition_key", "leachant", "NaOH_M", "acid_M", "CO2_condition",
            "condition_code", "reason_needed"], limit=MAX_LIST),
    }


def get_needed_simulations(ctx: RunContext) -> dict:
    """Conditions whose best candidate is *needs new simulation* (from the table)."""
    table = mapping_table.build_suggestion_table(
        ctx.data, ctx.manifest, ctx.condition_mapping, ctx.profile)
    needed = mapping_table.needs_new_simulation(table)
    return {
        "source": "get_needed_simulations",
        "n_needed": int(len(needed)),
        "conditions": _records(needed, [
            "condition_key", "n_replicates", "mapping_status", "reason"], limit=MAX_LIST),
    }


def get_bias_table(ctx: RunContext, min_n: int = residual_stats.DEFAULT_MIN_N) -> dict:
    """Prompt-13 systematic-bias table over exact-mapped residuals (statistics only)."""
    statuses = residual_stats.collect_sample_statuses(
        ctx.data, ctx.sample_mapping, ctx.comparison_df,
        manifest=ctx.manifest, profile=ctx.profile)
    table = residual_stats.bias_table(ctx.comparison_df, statuses, int(min_n),
                                      profile=ctx.profile)
    return {
        "source": "get_bias_table",
        "min_n": int(min_n),
        "non_claim": residual_stats.NON_CLAIM_LINE,
        "n_rows": int(len(table)),
        "n_sufficient": int(table["sufficient"].sum()) if not table.empty else 0,
        "rows": _records(table, limit=MAX_LIST),
    }


def get_comparison_rows(ctx: RunContext, limit: int = DEFAULT_ROWS) -> dict:
    """A capped slice of the comparison CSV (measured + model + residual columns)."""
    try:
        n = max(1, min(int(limit), MAX_ROWS))
    except (TypeError, ValueError):
        n = DEFAULT_ROWS
    elements = list(config.RESIDUAL_ELEMENTS)
    cols = (["sample_id", "CO2_condition", "NaOH_M", "time_min", "liquid_solid_ratio",
             "final_pH", "phreeqc_pH", "residual_pH"]
            + [f"{el}_mM" for el in elements]
            + [f"phreeqc_{el}_mM" for el in elements]
            + [f"residual_{el}" for el in elements])
    df = ctx.comparison_df
    return {
        "source": "get_comparison_rows",
        "n_total": int(len(df)),
        "n_returned": int(min(n, len(df))) if df is not None else 0,
        "capped": bool(df is not None and len(df) > n),
        "rows": _records(df, cols, limit=n),
    }


def get_run_provenance(ctx: RunContext) -> dict:
    """Prompt-1 provenance: is the stored comparison current, and if not, why."""
    try:
        is_current, reasons = run_manager.comparison_is_current(ctx.run_name)
    except Exception as exc:
        return {"source": "get_run_provenance", "error": str(exc),
                "is_current": False, "stale_reasons": ["provenance unavailable"]}
    meta = _safe(lambda: run_manager.read_comparison_meta(ctx.run_name), None)
    has_comp = _safe(lambda: run_manager.has_comparison(ctx.run_name), False)
    return {
        "source": "get_run_provenance",
        "has_comparison": bool(has_comp),
        "is_current": bool(is_current),
        "stale_reasons": list(reasons),
        "generated_at": (meta or {}).get("generated_at") if isinstance(meta, dict) else None,
    }


def get_mapping_trace(ctx: RunContext, condition_key: str) -> dict:
    """Prompt-6 scoring trace: representative sample + top scored candidates."""
    ck = str(condition_key or "").strip()
    if not ck:
        return {"source": "get_mapping_trace", "error": "condition_key is required."}
    sample, candidates = mapping_table.condition_candidates(
        ctx.data, ck, ctx.manifest, top_n=3, profile=ctx.profile)
    keep_sample = {k: sample.get(k) for k in (
        "sample_id", "leachant", "NaOH_M", "acid_M", "time_min", "CO2_condition",
        "liquid_solid_ratio", "temperature_C") if k in sample}
    out_candidates = []
    for c in candidates:
        out_candidates.append({
            "phreeqc_record_key": c.get("suggested_phreeqc_record_key", ""),
            "scenario_label": c.get("scenario_label", ""),
            "score": c.get("score"),
            "confidence": c.get("confidence"),
            "reason": c.get("reason"),
            "score_breakdown": c.get("score_breakdown"),
            "matched_fields": c.get("matched_fields"),
            "mismatched_fields": c.get("mismatched_fields"),
            "missing_metadata": c.get("missing_metadata"),
            "metadata_notes": c.get("metadata_notes"),
        })
    return {
        "source": "get_mapping_trace",
        "condition_key": ck,
        "representative_sample": _jsonable(keep_sample),
        "candidates": _jsonable(out_candidates),
    }


# Dispatch registry: tool name -> (callable(ctx, **kwargs)).
_TOOL_FUNCS = {
    "get_mapping_overview": lambda ctx, **kw: get_mapping_overview(ctx),
    "get_inclusion_counts": lambda ctx, **kw: get_inclusion_counts(ctx, kw.get("variable")),
    "get_unsafe_mappings": lambda ctx, **kw: get_unsafe_mappings(ctx),
    "get_needed_simulations": lambda ctx, **kw: get_needed_simulations(ctx),
    "get_bias_table": lambda ctx, **kw: get_bias_table(ctx, kw.get("min_n", residual_stats.DEFAULT_MIN_N)),
    "get_comparison_rows": lambda ctx, **kw: get_comparison_rows(ctx, kw.get("limit", DEFAULT_ROWS)),
    "get_run_provenance": lambda ctx, **kw: get_run_provenance(ctx),
    "get_mapping_trace": lambda ctx, **kw: get_mapping_trace(ctx, kw.get("condition_key", "")),
}


def dispatch(ctx: RunContext, name: str, tool_input: dict) -> dict:
    """Run one tool by name with its (validated) input; never raises into the loop."""
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return _jsonable(fn(ctx, **(tool_input or {})))
    except Exception as exc:  # a tool failure becomes data the model treats as unavailable
        return {"error": f"{name} failed: {exc}", "source": name}


# --------------------------------------------------------------------------- #
# Tool specs (advertised to the Anthropic API)
# --------------------------------------------------------------------------- #
def _obj(props=None, required=None) -> dict:
    return {"type": "object", "properties": props or {},
            "required": required or []}


TOOL_SPECS = [
    {"name": "get_mapping_overview",
     "description": "Condition-level mapping suggestion table + overall mapping status "
                    "(counts of exact / scenario-level / unsafe / needs-new, whether all are exact).",
     "input_schema": _obj()},
    {"name": "get_inclusion_counts",
     "description": "Per-variable comparison inclusion: rows plotted vs excluded, unique model "
                    "predictions, collapse warning, and the one overall validity status. Use this "
                    "to decide whether anything is 'validated' (only validity=='valid' is).",
     "input_schema": _obj({"variable": {"type": "string",
                          "description": "Optional comparison variable, e.g. final_pH or Ca_mM."}})},
    {"name": "get_unsafe_mappings",
     "description": "Saved condition mappings classified unsafe (known metadata conflicts, e.g. an "
                    "acid leachant mapped to a NaOH/CO2 scenario).",
     "input_schema": _obj()},
    {"name": "get_needed_simulations",
     "description": "Conditions whose best candidate is 'needs new simulation' (no suitable model result).",
     "input_schema": _obj()},
    {"name": "get_bias_table",
     "description": "Systematic-bias table (descriptive statistics) over EXACT-mapped residuals only: "
                    "per element/condition mean residual, std, sem, and a sufficient flag. Not a model.",
     "input_schema": _obj({"min_n": {"type": "integer",
                          "description": "Minimum exact pairs for a row to be 'sufficient' (default 5)."}})},
    {"name": "get_comparison_rows",
     "description": "A capped slice of the comparison table (measured, model prediction, and residual "
                    "columns per row). Use for specific per-sample values.",
     "input_schema": _obj({"limit": {"type": "integer",
                          "description": f"Rows to return (default {DEFAULT_ROWS}, max {MAX_ROWS})."}})},
    {"name": "get_run_provenance",
     "description": "Whether the stored comparison is still current with its inputs, and if not, the "
                    "stale reasons (data/mapping/PHREEQC changed since results were generated).",
     "input_schema": _obj()},
    {"name": "get_mapping_trace",
     "description": "The rule-based scoring trace for one condition: representative sample + top scored "
                    "candidates with score breakdown, matched/missing/conflicting fields, and notes.",
     "input_schema": _obj({"condition_key": {"type": "string",
                          "description": "The condition_key to explain (from get_mapping_overview)."}},
                         required=["condition_key"])},
]


# --------------------------------------------------------------------------- #
# Answer container + the agentic tool-use loop
# --------------------------------------------------------------------------- #
@dataclass
class AssistantAnswer:
    """A grounded answer + the auditable trace of tool calls behind it."""

    text: str
    trace: list = field(default_factory=list)
    ok: bool = True
    error: str | None = None


def _block_to_dict(block) -> dict:
    """Convert a response content block to the plain dict shape the API accepts back."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "") or ""}
    if btype == "tool_use":
        return {"type": "tool_use", "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""), "input": getattr(block, "input", {}) or {}}
    return {"type": "text", "text": ""}


def _summarize(name: str, out: dict) -> str:
    """A short, human-readable summary of a tool result for the 'data used' panel."""
    if not isinstance(out, dict):
        return str(out)[:300]
    if out.get("error"):
        return f"error: {out['error']}"[:300]
    keys = [k for k in out if k != "source"]
    blob = json.dumps({k: out[k] for k in keys}, default=str)
    return blob[:300] + ("…" if len(blob) > 300 else "")


def answer(ctx_or_run, question: str, *, client=None, model: str | None = None,
           history=None, max_iters: int = MAX_TOOL_ITERS, profile=None) -> AssistantAnswer:
    """Answer ``question`` about a run, grounded in read-only tool calls.

    ``ctx_or_run`` is a :class:`RunContext` or a run name (loaded via
    :meth:`RunContext.from_run`). ``history`` is an optional list of prior
    ``{"role","content"}`` text turns (session-only; never persisted). Returns an
    :class:`AssistantAnswer` with the final text and the tool-call ``trace``. When the
    assistant is disabled (no API key/SDK) it returns ``ok=False`` with a clean message
    and never raises.
    """
    ctx = ctx_or_run if isinstance(ctx_or_run, RunContext) else \
        RunContext.from_run(str(ctx_or_run), profile)

    client = _resolve_client(client)
    if client is None:
        return AssistantAnswer(
            text="", trace=[], ok=False,
            error="Assistant is disabled: set ANTHROPIC_API_KEY and install the anthropic SDK.")

    messages: list[dict] = []
    for turn in (history or []):
        role, content = turn.get("role"), turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)})
    messages.append({"role": "user", "content": str(question)})

    trace: list[dict] = []
    for _ in range(max(1, int(max_iters))):
        try:
            resp = client.messages.create(
                model=_model(model), max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
                tools=TOOL_SPECS, messages=messages)
        except Exception as exc:
            return AssistantAnswer(text="", trace=trace, ok=False, error=f"API error: {exc}")

        blocks = list(getattr(resp, "content", None) or [])
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            return AssistantAnswer(text=_message_text(resp), trace=trace, ok=True)

        messages.append({"role": "assistant", "content": [_block_to_dict(b) for b in blocks]})
        results = []
        for tu in tool_uses:
            name = getattr(tu, "name", "")
            tinput = getattr(tu, "input", {}) or {}
            out = dispatch(ctx, name, tinput)
            trace.append({"tool": name, "input": _jsonable(tinput),
                          "summary": _summarize(name, out)})
            results.append({"type": "tool_result", "tool_use_id": getattr(tu, "id", ""),
                            "content": json.dumps(out, default=str)})
        messages.append({"role": "user", "content": results})

    return AssistantAnswer(text="", trace=trace, ok=False,
                           error="Stopped: the assistant exceeded its tool-call budget.")
