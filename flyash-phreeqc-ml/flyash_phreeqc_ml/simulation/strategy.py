"""Simulation **strategy + ranking** layer for the Simulate workflow.

Lets a user say *what they want to optimise or estimate*, then ranks **already-executed**
simulation results against that objective and suggests a refined next sweep. It is
deliberately constrained and isolated:

* It **never runs simulations** and **never executes anything** — it only scores a result
  table the executor already produced. The objective is data, not an action.
* It is **deterministic** — objective parsing is rule-based here; it imports **no AI** and
  never invents a scientific output (the values come from PHREEQC; this layer only ranks
  them).
* It is **off the scientific result path** — it imports no comparison / residual / mapping
  module and touches no validation CSV. A ranking is an **optimisation over model
  predictions, not a validation** against measured data.

The result table it ranks is the one ``batch_executor.build_result_table`` produces
(columns ``scenario_id``, ``status``, ``pH``, ``pe``, ``<El>_mM``,
``leachant_concentration_M``, …).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

# Status string a rankable (executed-OK) row must carry. Mirrors phreeqc_executor.STATUS_SUCCESS
# without importing it, so this module stays dependency-free.
STATUS_SUCCESS = "success"

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
OBJ_MAXIMIZE = "maximize"               # maximise a target element (or pH/pe)
OBJ_MINIMIZE = "minimize"               # minimise an impurity element
OBJ_TARGET_PH = "target_ph"             # keep pH inside [low, high]
OBJ_AVOID_PH = "avoid_ph"               # keep pH OUTSIDE an unsafe [low, high]
OBJ_MINIMIZE_REAGENT = "minimize_reagent"   # minimise leachant concentration
OBJ_SELECTIVITY = "selectivity"         # maximise a ratio A/B
OBJ_WEIGHTED = "weighted"               # custom weighted score over >1 metric
OBJ_UNKNOWN = "unknown"                 # nothing detected — the UI asks the user

DIR_MAX = "max"
DIR_MIN = "min"
DIR_TARGET = "target"
DIR_AVOID = "avoid"

# Elements the text parser recognises (tokenising only; the real columns come from the table).
KNOWN_ELEMENTS = ("Ca", "Si", "Al", "Fe", "Na", "K", "Sc", "REE", "Ti", "V", "Mg", "Mn")
# When the text says "impurity" with no element, interpret it as these (if present).
DEFAULT_IMPURITY_ELEMENTS = ("Fe", "Al")
REAGENT_COLUMN = "leachant_concentration_M"
PH_COLUMN = "pH"

LARGE_SCALE_MESSAGE = ("Large-scale/adaptive search requires a dedicated batch backend or "
                       "surrogate-assisted workflow. This app currently supports small "
                       "confirmed sweeps.")
RANKING_NOT_VALIDATION = ("Ranking is based only on executed PHREEQC simulation outputs. It is "
                          "not validation against measured data.")
REFINEMENT_LABEL = ("This is a small reviewed refinement, not large-scale automatic "
                    "optimization.")
# Mirrors batch_executor.DEFAULT_MAX_SCENARIOS — kept local so this pure module never imports
# the executor (it must not be able to run anything).
DEFAULT_MAX_SCENARIOS = 20

# Per-axis physical floor (exclusive). A refined value must be strictly above it.
_PHYSICAL_FLOOR = {
    REAGENT_COLUMN: 0.0,        # a concentration must be > 0
    "time_min": 0.0,           # a reaction time must be > 0
    "liquid_solid_ratio": 0.0,
    "temperature_C": -273.15,  # above absolute zero (0 °C and a few °C are physical)
}


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #
@dataclass
class ObjectiveMetric:
    """One component of an objective: a column (or ``A_mM/B_mM`` ratio) + a direction."""

    column: str
    direction: str                       # DIR_MAX / DIR_MIN / DIR_TARGET / DIR_AVOID
    weight: float = 1.0
    target_low: float | None = None
    target_high: float | None = None
    label: str | None = None

    def display(self) -> str:
        base = self.label or self.column
        if self.direction == DIR_TARGET:
            return f"target {base} in [{self.target_low}, {self.target_high}]"
        if self.direction == DIR_AVOID:
            return f"avoid {base} in [{self.target_low}, {self.target_high}]"
        return f"{'maximise' if self.direction == DIR_MAX else 'minimise'} {base}"


@dataclass
class SimulationObjective:
    """What to optimise/estimate — a named kind plus its weighted metrics."""

    kind: str = OBJ_UNKNOWN
    metrics: list = field(default_factory=list)      # list[ObjectiveMetric]
    description: str = ""
    source: str = "rule"                             # "rule" / "manual" / "ai"
    notes: list = field(default_factory=list)

    @property
    def is_defined(self) -> bool:
        return bool(self.metrics)

    def display(self) -> str:
        if not self.metrics:
            return "no objective set"
        return "; ".join(m.display() for m in self.metrics)


@dataclass
class RankingResult:
    objective: SimulationObjective
    ranked: pd.DataFrame = field(default_factory=pd.DataFrame)
    status: str = "ranked"               # ranked / no_successful_rows / no_rankable_metrics
    driving_metric: str | None = None
    top_scenario_id: str | None = None
    used_metrics: list = field(default_factory=list)
    missing_metrics: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    tradeoffs: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ranked"


@dataclass
class RefinedSweepSuggestion:
    axis: str | None = None
    kind: str = "none"                   # extend_lower/extend_upper/refine_internal/
    #                                       narrow_failures/add_selected_output/define_sweep/none
    message: str = ""
    rationale: str = ""
    suggested_values: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Objective parsing (deterministic; no AI)
# --------------------------------------------------------------------------- #
_MAX_WORDS = r"maxim\w*|highest|most|increase\w*|greatest|best|high\b"
_MIN_WORDS = r"minim\w*|lowest|least|reduce\w*|decrease\w*|low\b|minimal"
_NUM = r"[-+]?\d*\.?\d+"


def _element_in(text: str) -> str | None:
    for el in KNOWN_ELEMENTS:
        if re.search(rf"\b{re.escape(el)}\b", text):
            return el
    return None


def _find_ph_range(text: str):
    """Detect 'pH 10 to 12' / 'pH 10-12' / 'pH between 10 and 12'. Returns (lo, hi) or None."""
    m = re.search(rf"ph\s*(?:of|=|:)?\s*(?:between\s*)?({_NUM})\s*(?:to|-|–|—|and|&)\s*({_NUM})",
                  text, re.I)
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    return (min(lo, hi), max(lo, hi))


def _find_ratio(text: str):
    """Detect a selectivity ratio 'A/B' or 'A to B ratio' or 'selectivity A over B'."""
    m = re.search(rf"\b({'|'.join(KNOWN_ELEMENTS)})\s*[/:]\s*({'|'.join(KNOWN_ELEMENTS)})\b", text)
    if m:
        return m.group(1), m.group(2)
    m = re.search(rf"selectivit\w*\s+(?:of\s+)?({'|'.join(KNOWN_ELEMENTS)})\s+(?:over|to|vs\.?)\s+"
                  rf"({'|'.join(KNOWN_ELEMENTS)})", text, re.I)
    if m:
        return m.group(1), m.group(2)
    return None


def parse_objective(desired_text: str, *, target_elements=None) -> SimulationObjective:
    """Extract a :class:`SimulationObjective` from the desired-outputs text (rule-based).

    Recognises: target pH ranges, avoid/unsafe pH, selectivity ratios, maximise/minimise of
    one or more elements (→ weighted when more than one), minimise reagent, and "impurity".
    Returns an ``OBJ_UNKNOWN`` objective with a note when nothing is detected — the UI then
    asks the user to set one. Never raises.
    """
    text = str(desired_text or "").strip()
    notes: list[str] = []
    if not text:
        return SimulationObjective(kind=OBJ_UNKNOWN, notes=["No desired-output text to parse."])

    low = text.lower()

    # 1) selectivity ratio (most specific)
    ratio = _find_ratio(text)
    if ratio:
        a, b = ratio
        m = ObjectiveMetric(column=f"{a}_mM/{b}_mM", direction=DIR_MAX, label=f"{a}/{b}")
        return SimulationObjective(kind=OBJ_SELECTIVITY, metrics=[m],
                                   description=f"maximise selectivity {a}/{b}", notes=notes)

    # 2) avoid / unsafe pH range
    if re.search(r"\b(avoid|unsafe|stay (?:out|away)|not)\b[^.]*\bph\b", low) or \
            re.search(r"\bph\b[^.]*\bunsafe\b", low):
        rng = _find_ph_range(text)
        if rng:
            m = ObjectiveMetric(PH_COLUMN, DIR_AVOID, target_low=rng[0], target_high=rng[1],
                                label="pH")
            return SimulationObjective(kind=OBJ_AVOID_PH, metrics=[m],
                                       description=f"avoid pH {rng[0]}–{rng[1]}", notes=notes)

    # 3) collect per-clause max/min targets
    clauses = re.split(r"\b(?:and|while|but|with|whilst|then)\b|[,;]", text, flags=re.I)
    max_targets: list[str] = []
    min_targets: list[str] = []
    reagent_min = False
    for clause in clauses:
        cl = clause.strip()
        if not cl:
            continue
        cl_low = cl.lower()
        is_max = bool(re.search(_MAX_WORDS, cl_low))
        is_min = bool(re.search(_MIN_WORDS, cl_low))
        if not (is_max or is_min):
            continue
        if re.search(r"\b(reagent|naoh|hcl|molarit\w*|dos\w*|concentration of (?:the )?(?:reagent|"
                     r"leachant))\b", cl_low) and is_min:
            reagent_min = True
            continue
        if "impurit" in cl_low and is_min:
            for el in DEFAULT_IMPURITY_ELEMENTS:
                if el not in min_targets:
                    min_targets.append(el)
            notes.append("Interpreted 'impurity' as " + "/".join(DEFAULT_IMPURITY_ELEMENTS)
                         + " (edit if your impurity differs).")
            continue
        el = _element_in(cl)
        if el is None:
            continue
        (max_targets if is_max else min_targets).append(el)

    # 4) target pH (only as a primary objective when no element targets were found)
    ph_range = _find_ph_range(text)

    metrics: list[ObjectiveMetric] = []
    for el in dict.fromkeys(max_targets):
        metrics.append(ObjectiveMetric(f"{el}_mM", DIR_MAX, label=el))
    for el in dict.fromkeys(min_targets):
        metrics.append(ObjectiveMetric(f"{el}_mM", DIR_MIN, label=el))
    if reagent_min:
        metrics.append(ObjectiveMetric(REAGENT_COLUMN, DIR_MIN, label="reagent concentration"))

    if len(metrics) >= 2:
        return SimulationObjective(kind=OBJ_WEIGHTED, metrics=metrics,
                                   description="weighted multi-metric objective", notes=notes)
    if len(metrics) == 1:
        only = metrics[0]
        kind = (OBJ_MINIMIZE_REAGENT if only.column == REAGENT_COLUMN
                else OBJ_MAXIMIZE if only.direction == DIR_MAX else OBJ_MINIMIZE)
        return SimulationObjective(kind=kind, metrics=metrics, description=only.display(),
                                   notes=notes)
    if ph_range:
        m = ObjectiveMetric(PH_COLUMN, DIR_TARGET, target_low=ph_range[0],
                            target_high=ph_range[1], label="pH")
        return SimulationObjective(kind=OBJ_TARGET_PH, metrics=[m],
                                   description=f"target pH {ph_range[0]}–{ph_range[1]}", notes=notes)

    notes.append("Could not determine an objective from the text — set one manually.")
    return SimulationObjective(kind=OBJ_UNKNOWN, notes=notes)


# --------------------------------------------------------------------------- #
# Convenience objective builders (used by the UI editor + tests)
# --------------------------------------------------------------------------- #
def maximize(element: str) -> SimulationObjective:
    return SimulationObjective(OBJ_MAXIMIZE, [ObjectiveMetric(f"{element}_mM", DIR_MAX,
                                                              label=element)], source="manual")


def minimize(element: str) -> SimulationObjective:
    return SimulationObjective(OBJ_MINIMIZE, [ObjectiveMetric(f"{element}_mM", DIR_MIN,
                                                              label=element)], source="manual")


def target_ph(low: float, high: float) -> SimulationObjective:
    return SimulationObjective(OBJ_TARGET_PH, [ObjectiveMetric(PH_COLUMN, DIR_TARGET,
                               target_low=low, target_high=high, label="pH")], source="manual")


def avoid_ph(low: float, high: float) -> SimulationObjective:
    return SimulationObjective(OBJ_AVOID_PH, [ObjectiveMetric(PH_COLUMN, DIR_AVOID,
                               target_low=low, target_high=high, label="pH")], source="manual")


def minimize_reagent() -> SimulationObjective:
    return SimulationObjective(OBJ_MINIMIZE_REAGENT, [ObjectiveMetric(REAGENT_COLUMN, DIR_MIN,
                               label="reagent concentration")], source="manual")


def selectivity(numerator: str, denominator: str) -> SimulationObjective:
    return SimulationObjective(OBJ_SELECTIVITY, [ObjectiveMetric(
        f"{numerator}_mM/{denominator}_mM", DIR_MAX, label=f"{numerator}/{denominator}")],
        source="manual")


def weighted(metrics) -> SimulationObjective:
    return SimulationObjective(OBJ_WEIGHTED, list(metrics), source="manual")


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def _resolve_series(table: pd.DataFrame, column: str):
    """A numeric series for a metric column or an ``A_mM/B_mM`` ratio. None if unavailable."""
    if "/" in column:
        num, den = [c.strip() for c in column.split("/", 1)]
        if num not in table.columns or den not in table.columns:
            return None
        n = pd.to_numeric(table[num], errors="coerce")
        d = pd.to_numeric(table[den], errors="coerce")
        return n.divide(d.where(d != 0))
    if column not in table.columns:
        return None
    return pd.to_numeric(table[column], errors="coerce")


def _metric_score(values: pd.Series, metric: ObjectiveMetric) -> pd.Series:
    """Per-row score in [0, 1] (higher = better) for one metric over the given values."""
    v = pd.to_numeric(values, errors="coerce")
    if metric.direction in (DIR_MAX, DIR_MIN):
        vmin, vmax = v.min(), v.max()
        if pd.isna(vmin) or pd.isna(vmax) or vmax == vmin:
            return pd.Series([0.5] * len(v), index=v.index)
        norm = (v - vmin) / (vmax - vmin)
        return norm if metric.direction == DIR_MAX else (1.0 - norm)
    lo = metric.target_low if metric.target_low is not None else v.min()
    hi = metric.target_high if metric.target_high is not None else v.max()
    span = (hi - lo) if (hi is not None and lo is not None and hi != lo) else 1.0
    if metric.direction == DIR_TARGET:
        dist = pd.Series(0.0, index=v.index)
        dist = dist.mask(v < lo, lo - v).mask(v > hi, v - hi)
        return (1.0 - (dist / abs(span))).clip(lower=0.0)
    # DIR_AVOID: inside the band is bad (0), outside is good (1)
    inside = (v >= lo) & (v <= hi)
    return pd.Series([0.0 if i else 1.0 for i in inside], index=v.index)


def rank_results(table: pd.DataFrame, objective: SimulationObjective, *,
                 axis_col: str | None = None) -> RankingResult:
    """Rank the executed (successful) rows of ``table`` against ``objective``.

    Computes a per-row weighted score (each metric normalised to [0, 1]), the rank, the
    metric that drove the top scenario, missing-metric warnings (never a false ranking on an
    absent column), and tradeoff notes. Never raises.
    """
    res = RankingResult(objective=objective)
    if table is None or table.empty or not objective.metrics:
        res.status = "no_rankable_metrics"
        res.warnings.append("No objective metrics to rank by." if not objective.metrics
                            else "No results to rank.")
        return res

    ok = table[table.get("status") == STATUS_SUCCESS].copy() if "status" in table.columns \
        else table.copy()
    if ok.empty:
        res.status = "no_successful_rows"
        res.warnings.append("No successfully-executed scenarios to rank.")
        return res

    usable: list[ObjectiveMetric] = []
    score = pd.Series(0.0, index=ok.index)
    weight_sum = 0.0
    mscore_cols: dict[int, str] = {}
    for i, m in enumerate(objective.metrics):
        series = _resolve_series(ok, m.column)
        if series is None or series.dropna().empty:
            res.missing_metrics.append(m.column)
            res.warnings.append(
                f"Requested metric '{m.label or m.column}' is not available in the executed "
                "results — it was not used for ranking.")
            continue
        ms = _metric_score(series, m).fillna(0.0)
        score = score + m.weight * ms
        weight_sum += m.weight
        usable.append(m)
        ok[m.column.replace("/", "_over_")] = series      # the metric's raw value (display)
        mcol = f"_mscore_{i}"
        ok[mcol] = ms                                     # the metric's [0,1] score (for driving)
        mscore_cols[id(m)] = mcol

    if not usable:
        res.status = "no_rankable_metrics"
        return res

    ok["score"] = (score / weight_sum) if weight_sum else score
    ok = ok.sort_values("score", ascending=False).reset_index(drop=True)
    ok["rank"] = range(1, len(ok) + 1)

    res.used_metrics = [m.column for m in usable]
    res.top_scenario_id = str(ok.iloc[0].get("scenario_id"))
    # Driving metric = the usable metric with the largest weighted score for the top scenario.
    top = ok.iloc[0]
    driver = max(usable, key=lambda m: m.weight * float(top.get(mscore_cols[id(m)], 0.0)))
    res.driving_metric = driver.label or driver.column
    res.tradeoffs = _tradeoff_notes(ok, usable)

    cols = ["scenario_id"]
    if axis_col and axis_col in ok.columns:
        cols.append(axis_col)
    cols += [c for c in (PH_COLUMN, "pe") if c in ok.columns]
    cols += [m.column.replace("/", "_over_") for m in usable]
    cols += ["score", "rank"]
    res.ranked = ok[[c for c in dict.fromkeys(cols) if c in ok.columns]].copy()
    return res


def _tradeoff_notes(ranked: pd.DataFrame, metrics) -> list:
    """Note when the top scenario is not also best on each individual metric."""
    if len(ranked) < 2 or len(metrics) < 2:
        return []
    notes: list[str] = []
    top_id = ranked.iloc[0].get("scenario_id")
    for m in metrics:
        col = m.column.replace("/", "_over_")
        if col not in ranked.columns:
            continue
        s = pd.to_numeric(ranked[col], errors="coerce")
        if s.dropna().empty:
            continue
        best_row = ranked.loc[s.idxmax() if m.direction in (DIR_MAX,) else s.idxmin()]
        if m.direction in (DIR_MAX, DIR_MIN) and best_row.get("scenario_id") != top_id:
            notes.append(
                f"Top scenario is best overall but not the "
                f"{'highest' if m.direction == DIR_MAX else 'lowest'} on "
                f"{m.label or m.column} (that is {best_row.get('scenario_id')}).")
    return notes[:3]


# --------------------------------------------------------------------------- #
# Refined sweep suggestion
# --------------------------------------------------------------------------- #
def _median_step(xs: list) -> float:
    sxs = sorted(xs)
    diffs = [b - a for a, b in zip(sxs, sxs[1:]) if b > a]
    if not diffs:
        return abs(sxs[0]) * 0.5 if sxs and sxs[0] else 1.0
    diffs.sort()
    return diffs[len(diffs) // 2]


def suggest_refined_sweep(ranking: RankingResult, table: pd.DataFrame,
                          axis_col: str | None) -> RefinedSweepSuggestion:
    """Propose a cautious next sweep from the ranking (never runs anything)."""
    sug = RefinedSweepSuggestion(axis=axis_col)

    n_total = len(table) if table is not None else 0
    n_fail = int((table.get("status") != STATUS_SUCCESS).sum()) if (
        table is not None and "status" in table.columns) else 0
    if n_total and n_fail / n_total >= 0.5:
        sug.kind = "narrow_failures"
        sug.message = ("Many scenarios failed — narrow the sweep range and re-check the PHREEQC "
                       "input assumptions (database, material composition, leachant template) "
                       "before sweeping wider.")
        sug.rationale = f"{n_fail}/{n_total} scenarios did not succeed."
        return sug

    if ranking.missing_metrics:
        sug.kind = "add_selected_output"
        sug.message = ("The requested metric(s) " + ", ".join(ranking.missing_metrics)
                       + " were not in the parsed outputs — add the matching SELECTED_OUTPUT "
                       "definitions (or parser support) so they can be ranked, then re-run.")
        sug.rationale = "Requested metrics were missing from the executed results."
        return sug

    if not ranking.ok or ranking.ranked is None or ranking.ranked.empty:
        sug.kind = "none"
        sug.message = "Nothing to refine yet — run a sweep that produces rankable results first."
        return sug

    if not axis_col or axis_col not in ranking.ranked.columns:
        sug.kind = "define_sweep"
        sug.message = ("Define a parameter sweep (e.g. vary concentration, time, or temperature) "
                       "so a trend can be ranked and refined.")
        sug.rationale = "No varying numeric sweep parameter was detected."
        return sug

    xs = pd.to_numeric(ranking.ranked[axis_col], errors="coerce").dropna().tolist()
    if len(set(xs)) < 2:
        sug.kind = "define_sweep"
        sug.message = ("Only one distinct value of the sweep parameter succeeded — sweep a few "
                       "values to find a trend.")
        return sug

    best_x = float(pd.to_numeric(ranking.ranked.iloc[0].get(axis_col)))
    lo, hi = min(xs), max(xs)
    step = _median_step(xs)

    if best_x <= lo:
        new = lo - step
        if axis_col == REAGENT_COLUMN or "concentration" in axis_col or "_M" in axis_col:
            new = max(new, round(lo / 2, 6))            # never below 0 for a concentration
        sug.kind = "extend_lower"
        sug.suggested_values = [round(new, 6)]
        sug.message = (f"The best result is at the **lower edge** ({axis_col}={best_x:g}). "
                       f"Cautiously extend the range downward — try {axis_col} ≈ {new:g}.")
        sug.rationale = "Optimum at the lower edge — the true optimum may lie below the range."
    elif best_x >= hi:
        new = hi + step
        sug.kind = "extend_upper"
        sug.suggested_values = [round(new, 6)]
        sug.message = (f"The best result is at the **upper edge** ({axis_col}={best_x:g}). "
                       f"Cautiously extend the range upward — try {axis_col} ≈ {new:g}.")
        sug.rationale = "Optimum at the upper edge — the true optimum may lie above the range."
    else:
        sxs = sorted(set(xs))
        i = sxs.index(min(sxs, key=lambda x: abs(x - best_x)))
        lower = sxs[i - 1] if i > 0 else best_x - step
        upper = sxs[i + 1] if i < len(sxs) - 1 else best_x + step
        mids = [round((lower + best_x) / 2, 6), round((best_x + upper) / 2, 6)]
        sug.kind = "refine_internal"
        sug.suggested_values = sorted({m for m in mids if m != best_x})
        sug.message = (f"The best result is **internal** ({axis_col}={best_x:g}). Refine with "
                       f"finer values around it — try {', '.join(f'{m:g}' for m in sug.suggested_values)}.")
        sug.rationale = "Optimum is inside the swept range — refine locally."
    return sug


# --------------------------------------------------------------------------- #
# Turn a suggestion into a concrete, physical, capped refined sweep (plan only)
# --------------------------------------------------------------------------- #
def physical_floor(axis: str | None):
    """The exclusive lower bound for an axis (None when there is no physical constraint)."""
    return _PHYSICAL_FLOOR.get(axis)


def is_physical_value(axis: str | None, value) -> bool:
    """True if ``value`` is a usable, physically-valid value for ``axis``."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if v != v:                                       # NaN
        return False
    floor = _PHYSICAL_FLOOR.get(axis)
    return True if floor is None else v > floor


@dataclass
class RefinedSweepPlan:
    """A concrete, reviewed-before-run refined sweep derived from a suggestion."""

    axis: str | None = None
    values: list = field(default_factory=list)       # the sweep values for the new matrix
    kind: str = "none"
    info: str = ""
    warnings: list = field(default_factory=list)
    blocked: bool = False                            # True → no matrix can be generated
    truncated: bool = False
    n_requested: int = 0

    @property
    def can_generate(self) -> bool:
        return not self.blocked and bool(self.values)


def _axis_values(ranking, table, axis) -> list:
    """The distinct successful axis values (from the ranking, else the table)."""
    vals: list = []
    ranked = getattr(ranking, "ranked", None)
    if ranked is not None and not getattr(ranked, "empty", True) and axis in ranked.columns:
        vals = pd.to_numeric(ranked[axis], errors="coerce").dropna().tolist()
    if not vals and table is not None and "status" in getattr(table, "columns", []) \
            and axis in table.columns:
        ok = table[table["status"] == STATUS_SUCCESS]
        vals = pd.to_numeric(ok[axis], errors="coerce").dropna().tolist()
    return sorted({float(v) for v in vals})


def refined_sweep_plan(suggestion: RefinedSweepSuggestion, ranking=None, table=None, *,
                       max_scenarios: int = DEFAULT_MAX_SCENARIOS) -> RefinedSweepPlan:
    """Deterministically convert a :class:`RefinedSweepSuggestion` into concrete sweep values.

    Rules: ``extend_upper`` adds higher values cautiously; ``extend_lower`` adds lower values
    but never nonphysical (≤0 concentration/time) ones; ``refine_internal`` makes a finer grid
    around the best region; ``narrow_failures`` proposes a smaller safe range;
    ``add_selected_output`` generates **no** chemistry values (it is blocked with a warning to
    improve the parser/SELECTED_OUTPUT). The result is capped at ``max_scenarios``. It is a
    **plan only** — this function runs nothing.
    """
    plan = RefinedSweepPlan(axis=suggestion.axis, kind=suggestion.kind)

    if suggestion.kind == "add_selected_output":
        plan.blocked = True
        plan.info = ("No new chemistry values are generated — add the SELECTED_OUTPUT / parser "
                     "definitions for the missing metric(s), then re-run the existing sweep.")
        return plan
    axis = suggestion.axis
    if suggestion.kind in ("none", "define_sweep") or not axis:
        plan.blocked = True
        plan.info = suggestion.message or "Define a sweep parameter first."
        return plan

    xs = _axis_values(ranking, table, axis)
    if not xs:
        plan.blocked = True
        plan.info = "No swept values to refine from."
        return plan
    step = _median_step(xs) if len(xs) >= 2 else (abs(xs[-1]) or 1.0) * 0.5

    raw: list = []
    if suggestion.kind == "extend_upper":
        hi = xs[-1]
        raw = [hi, hi + step, hi + 2 * step]
        plan.info = "Cautiously extended the range upward (two steps beyond the edge)."
    elif suggestion.kind == "extend_lower":
        lo = xs[0]
        cand = [lo - step, lo - 2 * step]
        phys = [c for c in cand if is_physical_value(axis, c)]
        if phys:
            raw = [lo] + phys
        else:
            raw = [lo, lo / 2.0]
            plan.warnings.append(
                f"A lower extension of {axis} would be nonphysical (≤ its floor) — used a halved "
                "value instead of a negative/zero one.")
        plan.info = "Cautiously extended the range downward, keeping every value physical."
    elif suggestion.kind == "refine_internal":
        ranked = getattr(ranking, "ranked", None)
        best = (float(ranked.iloc[0].get(axis)) if ranked is not None
                and not getattr(ranked, "empty", True) and axis in ranked.columns
                else xs[len(xs) // 2])
        i = xs.index(min(xs, key=lambda x: abs(x - best)))
        lower = xs[i - 1] if i > 0 else best - step
        upper = xs[i + 1] if i < len(xs) - 1 else best + step
        raw = [lower, (lower + best) / 2, best, (best + upper) / 2, upper]
        plan.info = "Refined with finer values around the best region."
    elif suggestion.kind == "narrow_failures":
        mid = xs[len(xs) // 2]
        raw = [mid * 0.75, mid, mid * 1.25]
        plan.info = "Narrowed to a smaller, safer range after many prior failures."
    else:
        plan.blocked = True
        plan.info = "No refinement available for this suggestion."
        return plan

    cleaned = sorted({round(float(v), 6) for v in raw if is_physical_value(axis, v)})
    if len(cleaned) < len(raw) and not any("nonphysical" in w for w in plan.warnings):
        plan.warnings.append(f"Dropped nonphysical value(s) for {axis}; kept only physical ones.")
    plan.n_requested = len(cleaned)
    if len(cleaned) > max_scenarios:
        plan.truncated = True
        plan.warnings.append(
            f"Refined sweep capped at {max_scenarios} scenarios (the small-sweep limit).")
        cleaned = cleaned[:max_scenarios]
    plan.values = cleaned
    if not plan.values:
        plan.blocked = True
        plan.info = "No physical refined values could be generated."
    return plan
