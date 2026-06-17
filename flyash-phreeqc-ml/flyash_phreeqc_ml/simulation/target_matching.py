"""**Target-matching / inverse simulation-search** layer for the Simulate workflow.

Lets a user say *what outputs they want* (a target pH range, a target element value, an
element to maximise/minimise, a constraint such as "Fe below 0.1 mM"), then build a small,
**reviewed, capped grid** of simulation scenarios over chosen parameters (leachant
concentration, release fraction, …) and — only on an explicit user action — run them and
**rank each candidate by how well it matches the target**. It is the *inverse* of the forward
planner: instead of "given inputs, what is the output", it asks "what inputs get close to the
output I want".

Like :mod:`strategy`, it is deliberately constrained and isolated:

* It is **pure + deterministic** — target parsing is rule-based; this module imports **no AI**
  and **no executor** (so it can never run anything itself). The grid is *plan data*; execution
  happens elsewhere on an explicit click, and this module only *scores* the result table the
  executor produced.
* It **never invents** a target value or a release fraction — every number comes from the
  user's text (parsed) or the search parameters the user selects + confirms.
* It is **off the scientific result path** — it imports no comparison / residual / mapping
  module and touches no validation CSV. A match ranking is **inverse search over model
  predictions, not validation** against measured data.

The result table it scores is the one ``batch_executor.build_result_table`` produces (columns
``scenario_id``, ``status``, ``pH``, ``pe``, ``<El>_mM``, ``leachant_concentration_M``, …).
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

import pandas as pd

# Status string a rankable (executed-OK) row must carry. Mirrors phreeqc_executor.STATUS_SUCCESS
# without importing it, so this module stays dependency-free (no executor → cannot run anything).
STATUS_SUCCESS = "success"
STATUS_PLAN_ONLY = "plan_only"

# --------------------------------------------------------------------------- #
# Vocabulary (kept local + minimal so this module imports only stdlib + pandas)
# --------------------------------------------------------------------------- #
# Target-metric kinds.
TARGET_RANGE = "target_range"        # keep a variable inside [low, high] (e.g. pH 10–12)
TARGET_VALUE = "target_value"        # hit a value within a tolerance (e.g. Ca ≈ 5 mM)
TARGET_MAXIMIZE = "maximize"         # maximise a variable (relative across candidates)
TARGET_MINIMIZE = "minimize"         # minimise a variable (relative across candidates)
TARGET_CONSTRAINT = "constraint"     # a hard bound that gates feasibility (e.g. Fe < 0.1 mM)
OBJECTIVE_KINDS = (TARGET_RANGE, TARGET_VALUE, TARGET_MAXIMIZE, TARGET_MINIMIZE)

# Constraint operators.
OP_LT, OP_LE, OP_GT, OP_GE = "<", "<=", ">", ">="

# Search-parameter kinds.
PARAM_SCENARIO = "scenario"                 # a flat scenario field (leachant_concentration_M, …)
PARAM_RELEASE_FRACTION = "release_fraction"  # the global dissolution fraction (0–1)

# Elements the text parser recognises (the *columns* still come from the executed table).
KNOWN_ELEMENTS = ("Ca", "Si", "Al", "Fe", "Na", "K", "Sc", "REE", "Ti", "V", "Mg", "Mn")
PH_COLUMN = "pH"
REAGENT_COLUMN = "leachant_concentration_M"
RELEASE_FRACTION_COLUMN = "release_fraction"

# Mirrors batch_executor.DEFAULT_MAX_SCENARIOS — kept local so this pure module never imports
# the executor (it must not be able to run anything). The grid is capped at this.
DEFAULT_MAX_SCENARIOS = 20

# Match statuses.
MATCH_RANKED = "ranked"
MATCH_NO_ROWS = "no_successful_rows"
MATCH_NO_METRICS = "no_metrics"

NOT_VALIDATION = ("Target matching is inverse search over model predictions under your chosen "
                  "assumptions (parameter ranges, release fractions, database, phases). It is "
                  "NOT validation against measured data.")
LARGE_SEARCH_MESSAGE = ("Large-scale / global inverse search (many parameters, fine grids, "
                        "adaptive optimisation) needs a dedicated optimisation backend or "
                        "surrogate. This app supports a small, reviewed, capped grid only.")
DEPENDS_ON_RANGES = ("The best match depends entirely on the parameter ranges you chose — it is "
                     "the best within this grid, not a global optimum.")
RELEASE_FRACTION_ASSUMPTION = ("Release fractions are USER ASSUMPTIONS, not measured truth — they "
                               "directly control the predicted dissolved totals.")
SCORING_METHOD_NOTE = (
    "Each objective metric is scored in [0, 1]: a target RANGE scores 1 inside the band with a "
    "linear falloff outside; a target VALUE scores 1 at the value with a linear falloff over the "
    "tolerance; MAXIMIZE/MINIMIZE are min-max normalised across the candidates. The weighted mean "
    "of the objective metrics is the objective score. CONSTRAINTS gate feasibility (a candidate is "
    "feasible only if every checkable constraint holds). Candidates are ranked feasible-first, "
    "then by objective score. A metric whose column is absent from the executed results is NOT "
    "scored (no value is fabricated).")

_NUM = r"[-+]?\d*\.?\d+"


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #
@dataclass
class TargetMetric:
    """One component of a target: a column (``pH`` / ``<El>_mM`` / a parameter) + how to score it."""

    kind: str                            # TARGET_*
    column: str                          # "pH" or "<El>_mM" (or a parameter column)
    label: str
    low: float | None = None             # TARGET_RANGE
    high: float | None = None            # TARGET_RANGE
    value: float | None = None           # TARGET_VALUE
    tolerance: float | None = None       # TARGET_VALUE
    op: str | None = None                # TARGET_CONSTRAINT operator
    threshold: float | None = None       # TARGET_CONSTRAINT bound
    weight: float = 1.0

    @property
    def is_constraint(self) -> bool:
        return self.kind == TARGET_CONSTRAINT

    def display(self) -> str:
        if self.kind == TARGET_RANGE:
            return f"target {self.label} in [{_g(self.low)}, {_g(self.high)}]"
        if self.kind == TARGET_VALUE:
            return f"target {self.label} ≈ {_g(self.value)} (±{_g(self.tolerance)})"
        if self.kind == TARGET_CONSTRAINT:
            return f"{self.label} {self.op} {_g(self.threshold)}"
        if self.kind == TARGET_MAXIMIZE:
            return f"maximise {self.label}"
        if self.kind == TARGET_MINIMIZE:
            return f"minimise {self.label}"
        return self.label

    def to_dict(self) -> dict:
        return {"kind": self.kind, "column": self.column, "label": self.label, "low": self.low,
                "high": self.high, "value": self.value, "tolerance": self.tolerance,
                "op": self.op, "threshold": self.threshold, "weight": self.weight}


@dataclass
class TargetSpec:
    """What the user wants — a set of objective metrics + hard constraints."""

    metrics: list = field(default_factory=list)      # list[TargetMetric]
    description: str = ""
    source: str = "rule"                             # "rule" / "manual" / "ai"
    notes: list = field(default_factory=list)

    @property
    def is_defined(self) -> bool:
        return bool(self.metrics)

    @property
    def objective_metrics(self) -> list:
        return [m for m in self.metrics if not m.is_constraint]

    @property
    def constraint_metrics(self) -> list:
        return [m for m in self.metrics if m.is_constraint]

    def display(self) -> str:
        return "; ".join(m.display() for m in self.metrics) if self.metrics else "no target set"

    def required_columns(self) -> list:
        return list(dict.fromkeys(m.column for m in self.metrics))

    def to_dict(self) -> dict:
        return {"description": self.display(), "source": self.source,
                "metrics": [m.to_dict() for m in self.metrics], "notes": list(self.notes)}


@dataclass
class SearchParameter:
    """One axis of the inverse search — a list of reviewed values to try."""

    name: str                            # flat scenario field OR "release_fraction"
    values: list                         # list[float]
    kind: str = PARAM_SCENARIO
    label: str | None = None

    def display_label(self) -> str:
        return self.label or self.name

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "label": self.display_label(),
                "values": list(self.values)}


@dataclass
class CandidateScenario:
    """One reviewed grid point — a scenario override + an (optional) release fraction."""

    scenario_id: str
    parent_label: str
    varied: dict                         # {parameter_label: value}
    scenario_flat: dict                  # the full flat scenario (base + overrides)
    leachant_concentration_M: float | None = None
    release_fraction: float | None = None
    status: str = STATUS_PLAN_ONLY

    def metadata_row(self) -> dict:
        """A metadata row for ``batch_executor.build_result_table`` + the result join."""
        return {
            "scenario_id": self.scenario_id,
            "leachant_type": self.scenario_flat.get("leachant_type"),
            "leachant_concentration_M": self.leachant_concentration_M,
            "time_min": self.scenario_flat.get("time_min"),
            "temperature_C": self.scenario_flat.get("temperature_C"),
            RELEASE_FRACTION_COLUMN: self.release_fraction,
        }


@dataclass
class MatchScoreBreakdown:
    """Per-candidate score detail (the 'why' behind a row's rank)."""

    scenario_id: str
    objective_score: float
    feasible: bool
    rank: int | None = None
    metric_scores: list = field(default_factory=list)        # list[dict]
    constraint_results: list = field(default_factory=list)   # list[dict]


@dataclass
class TargetMatchResult:
    """The outcome of scoring an executed result table against a :class:`TargetSpec`."""

    target_spec: TargetSpec
    ranked: pd.DataFrame = field(default_factory=pd.DataFrame)
    breakdowns: list = field(default_factory=list)           # list[MatchScoreBreakdown]
    best: dict | None = None
    status: str = MATCH_RANKED
    used_metrics: list = field(default_factory=list)
    missing_metrics: list = field(default_factory=list)
    n_candidates: int = 0
    n_feasible: int = 0
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == MATCH_RANKED


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _g(v) -> str:
    """Compact number formatting that tolerates None."""
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _num_or_none(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f                                  # drop NaN


def _token_pattern() -> str:
    """A regex alternation matching ``pH`` or a known element (longest-first)."""
    toks = ["pH"] + sorted(KNOWN_ELEMENTS, key=len, reverse=True)
    return "(" + "|".join(re.escape(t) for t in toks) + ")"


_TOKEN = _token_pattern()


def _column_for_token(tok: str) -> tuple:
    """Map a matched token to its (column, label): ``pH`` → (pH, pH); ``ca`` → (Ca_mM, Ca)."""
    if tok.lower() == "ph":
        return PH_COLUMN, "pH"
    for el in KNOWN_ELEMENTS:
        if el.lower() == tok.lower():
            return f"{el}_mM", el
    return f"{tok}_mM", tok


def _default_tolerance(column: str, value: float) -> float:
    """A sensible default ± for a target value (editable in the UI)."""
    if column == PH_COLUMN:
        return 0.5
    return max(abs(float(value)) * 0.2, 0.5)


# --------------------------------------------------------------------------- #
# Target parsing (deterministic; no AI). Priority: pH range > constraint > value > max/min.
# --------------------------------------------------------------------------- #
_PH_RANGE_RE = re.compile(
    rf"ph\s*(?:of|=|:)?\s*(?:between\s*)?({_NUM})\s*(?:to|-|–|—|and|&)\s*({_NUM})", re.I)
_LESS_OPS = ("<=", "<", "below", "under", "less than", "at most", "no more than", "beneath")
_MORE_OPS = (">=", ">", "above", "over", "greater than", "more than", "at least")
_LESS_RE = re.compile(
    rf"\b{_TOKEN}\b\s*(?:is\s*|of\s*|to\s*be\s*|should\s*be\s*|stay\s*|kept\s*)?"
    r"(<=|<|below|under|less than|at most|no more than|beneath)\s*"
    rf"({_NUM})", re.I)
_MORE_RE = re.compile(
    rf"\b{_TOKEN}\b\s*(?:is\s*|of\s*|to\s*be\s*|should\s*be\s*|stay\s*|kept\s*)?"
    r"(>=|>|above|over|greater than|more than|at least)\s*"
    rf"({_NUM})", re.I)
_VALUE_RE = re.compile(
    rf"\b{_TOKEN}\b\s*"
    r"(?:=|≈|~|around|near|about|approx\w*|close to|roughly|of|to be|equal to)\s*"
    rf"(?:of\s*)?({_NUM})", re.I)
_TARGET_VALUE_RE = re.compile(
    rf"\btarget(?:ing|ed)?\b\s+{_TOKEN}\b\s*(?:=|of|at|near|around|to|to\s*be)?\s*({_NUM})", re.I)
_MAX_WORDS = re.compile(r"maxim\w*|highest|most|increase\w*|greatest|\bhigh\b", re.I)
_MIN_WORDS = re.compile(r"minim\w*|lowest|least|reduce\w*|decrease\w*|\blow\b|minimal", re.I)
_REAGENT_RE = re.compile(r"\b(reagent|naoh|hcl|molarit\w*|dos\w*|concentration)\b", re.I)
_CLAUSE_SPLIT = re.compile(r"\b(?:and|while|whilst|but|with|then|keeping|keep)\b|[,;]", re.I)


def _find_ph_range(text: str):
    m = _PH_RANGE_RE.search(text)
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    return (min(lo, hi), max(lo, hi))


def _find_constraints(text: str) -> list:
    out = []
    for rx, ops in ((_LESS_RE, _LESS_OPS), (_MORE_RE, _MORE_OPS)):
        for m in rx.finditer(text):
            col, label = _column_for_token(m.group(1))
            word = m.group(2).lower()
            if rx is _LESS_RE:
                op = OP_LE if word in ("<=", "at most", "no more than") else OP_LT
            else:
                op = OP_GE if word in (">=", "at least") else OP_GT
            out.append((col, label, op, float(m.group(3))))
    return out


def _find_target_values(text: str) -> list:
    out = []
    for m in _VALUE_RE.finditer(text):
        col, label = _column_for_token(m.group(1))
        out.append((col, label, float(m.group(2))))
    for m in _TARGET_VALUE_RE.finditer(text):
        col, label = _column_for_token(m.group(1))
        out.append((col, label, float(m.group(2))))
    return out


def _first_element_column(text: str) -> tuple:
    m = re.search(rf"\b{_TOKEN}\b", text, re.I)
    return _column_for_token(m.group(1)) if m else (None, None)


def _find_max_min(text: str) -> list:
    out = []
    for clause in _CLAUSE_SPLIT.split(text):
        cl = (clause or "").strip()
        if not cl:
            continue
        is_max = bool(_MAX_WORDS.search(cl))
        is_min = bool(_MIN_WORDS.search(cl))
        if not (is_max or is_min):
            continue
        if is_min and _REAGENT_RE.search(cl) and _first_element_column(cl)[0] in (None, PH_COLUMN):
            out.append((REAGENT_COLUMN, "reagent concentration", TARGET_MINIMIZE))
            continue
        col, label = _first_element_column(cl)
        if col is None:
            continue
        out.append((col, label, TARGET_MAXIMIZE if is_max else TARGET_MINIMIZE))
    return out


def parse_target_spec(text: str, *, source: str = "rule") -> TargetSpec:
    """Extract a :class:`TargetSpec` from desired-output text (rule-based, deterministic).

    Recognises (in priority order, never double-counting a column): a target **pH range**
    ("pH 10 to 12"); **constraints** ("Fe below 0.1 mM", "Si < 5 mM", "keep Al above 1");
    **target values** ("Ca around 5 mM", "pH near 12"); and **maximise / minimise** of an
    element ("maximise Si", "lowest Fe", "low Al"). Returns an empty spec with a note when
    nothing is detected — the UI then asks the user to set one. Never raises, never invents.
    """
    raw = str(text or "").strip()
    metrics: list = []
    notes: list = []
    used: set = set()
    if not raw:
        notes.append("No desired-output text to parse — set a target manually.")
        return TargetSpec(metrics=[], source=source, notes=notes)

    rng = _find_ph_range(raw)
    if rng:
        metrics.append(TargetMetric(TARGET_RANGE, PH_COLUMN, "pH", low=rng[0], high=rng[1]))
        used.add(PH_COLUMN)

    for col, label, op, num in _find_constraints(raw):
        if col in used:
            continue
        metrics.append(TargetMetric(TARGET_CONSTRAINT, col, label, op=op, threshold=num))
        used.add(col)

    for col, label, num in _find_target_values(raw):
        if col in used:
            continue
        metrics.append(TargetMetric(TARGET_VALUE, col, label, value=num,
                                    tolerance=_default_tolerance(col, num)))
        used.add(col)

    for col, label, direction in _find_max_min(raw):
        if col in used:
            continue
        metrics.append(TargetMetric(direction, col, label))
        used.add(col)

    if not metrics:
        notes.append("Could not determine a target from the text — set one manually below.")
    spec = TargetSpec(metrics=metrics, source=source, notes=notes)
    spec.description = spec.display()
    return spec


# --------------------------------------------------------------------------- #
# Manual metric / spec / parameter builders (used by the UI editor + tests)
# --------------------------------------------------------------------------- #
def metric_range(column: str, low: float, high: float, *, label: str | None = None) -> TargetMetric:
    return TargetMetric(TARGET_RANGE, column, label or column, low=float(low), high=float(high))


def metric_value(column: str, value: float, *, tolerance: float | None = None,
                 label: str | None = None) -> TargetMetric:
    return TargetMetric(TARGET_VALUE, column, label or column, value=float(value),
                        tolerance=float(tolerance) if tolerance is not None
                        else _default_tolerance(column, value))


def metric_maximize(column: str, *, label: str | None = None) -> TargetMetric:
    return TargetMetric(TARGET_MAXIMIZE, column, label or column)


def metric_minimize(column: str, *, label: str | None = None) -> TargetMetric:
    return TargetMetric(TARGET_MINIMIZE, column, label or column)


def metric_constraint(column: str, op: str, threshold: float, *,
                      label: str | None = None) -> TargetMetric:
    return TargetMetric(TARGET_CONSTRAINT, column, label or column, op=op,
                        threshold=float(threshold))


def target_from_metrics(metrics, *, source: str = "manual", description: str = "") -> TargetSpec:
    spec = TargetSpec(metrics=list(metrics), source=source)
    spec.description = description or spec.display()
    return spec


def scenario_parameter(name: str, values, *, label: str | None = None) -> SearchParameter:
    vals = [v for v in (_num_or_none(x) for x in values) if v is not None]
    return SearchParameter(name=name, values=vals, kind=PARAM_SCENARIO, label=label)


def release_fraction_parameter(values) -> SearchParameter:
    vals = [v for v in (_num_or_none(x) for x in values) if v is not None]
    return SearchParameter(name=RELEASE_FRACTION_COLUMN, values=vals,
                           kind=PARAM_RELEASE_FRACTION, label="release fraction")


# --------------------------------------------------------------------------- #
# Build the (capped) search grid — plan only; this runs nothing
# --------------------------------------------------------------------------- #
def build_search_grid(base_scenario, parameters, *, max_scenarios: int = DEFAULT_MAX_SCENARIOS,
                      id_prefix: str = "TGT") -> tuple:
    """Cartesian product of the reviewed ``parameters`` over ``base_scenario`` → candidates.

    Returns ``(candidates, truncated)``. Every candidate is ``status=plan_only`` — **no
    PHREEQC is run here** (this module imports no executor). A scenario-field parameter
    overrides that field; a ``release_fraction`` parameter sets the candidate's release
    fraction (consumed by the source-term model when the UI builds the input). The product is
    capped at ``max_scenarios`` (excess dropped with ``truncated=True``, never silently).
    """
    params = [p for p in (parameters or []) if getattr(p, "values", None)]
    if base_scenario is None or not params:
        return [], False
    base_flat = dict(base_scenario.to_flat_dict())
    parent = base_flat.get("material_name") or base_flat.get("material_type") or "scenario"

    combos = list(itertools.product(*[list(p.values) for p in params]))
    cap = max(0, int(max_scenarios))
    truncated = len(combos) > cap
    combos = combos[:cap]

    candidates: list = []
    for i, combo in enumerate(combos, start=1):
        flat = dict(base_flat)
        varied: dict = {}
        release_fraction = None
        for p, val in zip(params, combo):
            varied[p.display_label()] = val
            if p.kind == PARAM_RELEASE_FRACTION:
                release_fraction = val
            else:
                flat[p.name] = val
        candidates.append(CandidateScenario(
            scenario_id=f"{id_prefix}-{i:03d}", parent_label=str(parent), varied=varied,
            scenario_flat=flat, leachant_concentration_M=flat.get("leachant_concentration_M"),
            release_fraction=release_fraction))
    return candidates, truncated


def grid_preview_frame(candidates) -> pd.DataFrame:
    """A compact preview table of the candidate grid (plan-only)."""
    rows = [{
        "scenario_id": c.scenario_id,
        "leachant_type": c.scenario_flat.get("leachant_type"),
        "leachant_concentration_M": c.leachant_concentration_M,
        RELEASE_FRACTION_COLUMN: c.release_fraction,
        "time_min": c.scenario_flat.get("time_min"),
        "temperature_C": c.scenario_flat.get("temperature_C"),
        "status": c.status,
    } for c in (candidates or [])]
    return pd.DataFrame(rows)


def candidate_metadata_frame(candidates) -> pd.DataFrame:
    """The metadata frame to pass as ``batch_executor.build_result_table(batch, matrix=…)``."""
    return pd.DataFrame([c.metadata_row() for c in (candidates or [])])


# --------------------------------------------------------------------------- #
# Scoring (pure; consumes an executed result table)
# --------------------------------------------------------------------------- #
def _objective_score(values: pd.Series, metric: TargetMetric) -> pd.Series:
    """Per-row score in [0, 1] (higher = better) for one objective metric."""
    v = pd.to_numeric(values, errors="coerce")
    if metric.kind in (TARGET_MAXIMIZE, TARGET_MINIMIZE):
        vmin, vmax = v.min(), v.max()
        if pd.isna(vmin) or pd.isna(vmax) or vmax == vmin:
            return pd.Series([0.5] * len(v), index=v.index)
        norm = (v - vmin) / (vmax - vmin)
        return norm if metric.kind == TARGET_MAXIMIZE else (1.0 - norm)
    if metric.kind == TARGET_RANGE:
        lo = metric.low if metric.low is not None else v.min()
        hi = metric.high if metric.high is not None else v.max()
        span = (hi - lo) if (hi is not None and lo is not None and hi != lo) else 1.0
        dist = pd.Series(0.0, index=v.index)
        dist = dist.mask(v < lo, lo - v).mask(v > hi, v - hi)
        return (1.0 - dist / abs(span)).clip(lower=0.0)
    if metric.kind == TARGET_VALUE:
        if metric.value is None:                              # incompletely-specified target
            return pd.Series([0.5] * len(v), index=v.index)
        tol = metric.tolerance if metric.tolerance not in (None, 0) else 1.0
        return (1.0 - (v - metric.value).abs() / abs(tol)).clip(lower=0.0)
    return pd.Series([0.0] * len(v), index=v.index)


def _constraint_satisfied(value, metric: TargetMetric):
    """True/False, or None when the value/bound is unavailable (never fabricates a verdict)."""
    f = _num_or_none(value)
    if f is None or metric.threshold is None or metric.op is None:
        return None
    t = metric.threshold
    return {OP_LT: f < t, OP_LE: f <= t, OP_GT: f > t, OP_GE: f >= t}.get(metric.op)


def score_results(target_spec: TargetSpec, table: pd.DataFrame) -> TargetMatchResult:
    """Score + rank executed simulation rows against ``target_spec``.

    Per candidate: a weighted objective score (each metric in [0, 1]) and a feasibility flag
    from the hard constraints. Ranks **feasible-first, then by objective score**. A target
    metric whose column is absent from the executed results is reported in ``missing_metrics``
    and **not scored** (no fabricated value); a constraint on a missing column cannot be checked
    and does **not** penalise a row (it is flagged instead). Never raises.
    """
    res = TargetMatchResult(target_spec=target_spec)
    if target_spec is None or not target_spec.metrics:
        res.status = MATCH_NO_METRICS
        res.warnings.append("No target metrics defined — set a target first.")
        return res
    if table is None or getattr(table, "empty", True):
        res.status = MATCH_NO_ROWS
        res.warnings.append("No simulation results to match against.")
        return res

    ok = (table[table.get("status") == STATUS_SUCCESS].copy()
          if "status" in table.columns else table.copy())
    res.n_candidates = len(table)
    if ok.empty:
        res.status = MATCH_NO_ROWS
        res.warnings.append("No successfully-executed scenarios to match.")
        return res
    ok = ok.reset_index(drop=True)

    # --- objective metrics → weighted [0,1] score ------------------------- #
    obj_score = pd.Series(0.0, index=ok.index)
    weight_sum = 0.0
    used: list = []                                  # list[(metric, score_col)]
    for i, m in enumerate(target_spec.objective_metrics):
        if m.column not in ok.columns or pd.to_numeric(ok[m.column], errors="coerce").dropna().empty:
            res.missing_metrics.append(m.column)
            res.warnings.append(
                f"Target metric '{m.label}' ({m.column}) is not in the executed results — it was "
                "not scored (no value was fabricated).")
            continue
        ms = _objective_score(ok[m.column], m).fillna(0.0)
        scol = f"_objscore_{i}"
        ok[scol] = ms
        obj_score = obj_score + m.weight * ms
        weight_sum += m.weight
        used.append((m, scol))

    ok["objective_score"] = (obj_score / weight_sum) if weight_sum else pd.Series(
        [1.0] * len(ok), index=ok.index)        # pure-constraint search → any feasible row is best

    # --- constraints → feasibility ---------------------------------------- #
    feasible = pd.Series(True, index=ok.index)
    con_used: list = []                              # list[(metric, sat_col)]
    for j, m in enumerate(target_spec.constraint_metrics):
        if m.column not in ok.columns or pd.to_numeric(ok[m.column], errors="coerce").dropna().empty:
            res.missing_metrics.append(m.column)
            res.warnings.append(
                f"Constraint '{m.display()}' references {m.column}, which is not in the executed "
                "results — it could not be checked (rows are not penalised for an unmeasurable "
                "constraint).")
            continue
        ccol = f"_consat_{j}"
        ok[ccol] = ok[m.column].map(lambda x, _m=m: _constraint_satisfied(x, _m))
        feasible = feasible & ok[ccol].map(lambda s: True if s is None else bool(s))
        con_used.append((m, ccol))
    ok["feasible"] = feasible

    res.used_metrics = [m.column for m, _ in used] + [m.column for m, _ in con_used]
    res.missing_metrics = list(dict.fromkeys(res.missing_metrics))
    if not used and not con_used:
        res.status = MATCH_NO_METRICS
        res.warnings.append(
            "None of the target metrics are present in the executed results — nothing could be "
            "scored. Add the matching SELECTED_OUTPUT / parser support and re-run.")
        return res

    # --- rank: feasible-first, then objective score ----------------------- #
    ok = ok.sort_values(["feasible", "objective_score"], ascending=[False, False],
                        kind="mergesort").reset_index(drop=True)
    ok["rank"] = range(1, len(ok) + 1)
    res.n_feasible = int(ok["feasible"].sum())

    # --- per-candidate breakdowns ----------------------------------------- #
    for _, row in ok.iterrows():
        res.breakdowns.append(MatchScoreBreakdown(
            scenario_id=str(row.get("scenario_id")),
            objective_score=round(float(row.get("objective_score") or 0.0), 4),
            feasible=bool(row.get("feasible")), rank=int(row.get("rank")),
            metric_scores=[{
                "label": m.label, "kind": m.kind, "column": m.column,
                "value": _num_or_none(row.get(m.column)),
                "score": round(float(row.get(scol) or 0.0), 4), "weight": m.weight,
            } for m, scol in used],
            constraint_results=[{
                "label": m.label, "display": m.display(), "column": m.column, "op": m.op,
                "threshold": m.threshold, "value": _num_or_none(row.get(m.column)),
                "satisfied": (None if row.get(ccol) is None or
                              (isinstance(row.get(ccol), float) and pd.isna(row.get(ccol)))
                              else bool(row.get(ccol))),
            } for m, ccol in con_used]))

    # --- best candidate + clean ranked frame ------------------------------ #
    top = ok.iloc[0]
    res.best = {
        "scenario_id": str(top.get("scenario_id")),
        "objective_score": round(float(top.get("objective_score") or 0.0), 4),
        "feasible": bool(top.get("feasible")),
        "leachant_concentration_M": _num_or_none(top.get("leachant_concentration_M")),
        "release_fraction": _num_or_none(top.get(RELEASE_FRACTION_COLUMN)),
        "values": {m.column: _num_or_none(top.get(m.column)) for m, _ in used},
    }
    if res.n_feasible == 0 and con_used:
        res.warnings.append(
            "No candidate satisfies all constraints — the top-ranked scenario still violates at "
            "least one. Loosen a constraint or widen the search grid.")

    display_cols = ["scenario_id"]
    for c in (REAGENT_COLUMN, RELEASE_FRACTION_COLUMN, "time_min", "temperature_C"):
        if c in ok.columns and ok[c].notna().any():
            display_cols.append(c)
    display_cols += [c for c in (PH_COLUMN, "pe") if c in ok.columns]
    for m, _ in used + con_used:
        if m.column in ok.columns and m.column not in display_cols:
            display_cols.append(m.column)
    display_cols += ["objective_score", "feasible", "rank"]
    res.ranked = ok[[c for c in dict.fromkeys(display_cols) if c in ok.columns]].copy()
    res.ranked["objective_score"] = pd.to_numeric(res.ranked["objective_score"],
                                                   errors="coerce").round(4)
    res.status = MATCH_RANKED
    return res


# --------------------------------------------------------------------------- #
# Provenance (mirrors run_registry's `refinement` field; pure dict, no I/O)
# --------------------------------------------------------------------------- #
def target_match_provenance(target_spec, parameters, candidates, match_result, *,
                            created_at: str | None = None, max_scenarios: int = DEFAULT_MAX_SCENARIOS,
                            truncated: bool = False) -> dict:
    """A JSON-safe provenance dict for a saved target-matching search (never validation)."""
    return {
        "target_spec": target_spec.to_dict() if target_spec is not None else None,
        "search_parameters": [p.to_dict() for p in (parameters or [])],
        "candidate_grid": grid_preview_frame(candidates).to_dict("records"),
        "n_candidates": len(candidates or []),
        "max_scenarios": int(max_scenarios),
        "grid_truncated": bool(truncated),
        "scoring_method": SCORING_METHOD_NOTE,
        "best_candidate": (match_result.best if match_result is not None else None),
        "n_feasible": (match_result.n_feasible if match_result is not None else 0),
        "missing_metrics": (list(match_result.missing_metrics) if match_result is not None else []),
        "warnings": (list(match_result.warnings) if match_result is not None else []),
        "not_validation": NOT_VALIDATION,
        "depends_on_ranges": DEPENDS_ON_RANGES,
        "created_at": created_at,
    }
