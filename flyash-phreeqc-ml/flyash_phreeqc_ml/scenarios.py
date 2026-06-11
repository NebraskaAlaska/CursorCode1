"""PHREEQC scenario manifest + rule-based mapping assistant.

This module helps a user decide *which* PHREEQC result row a measured sample
should be mapped to, instead of blindly picking from a dropdown. It does **no
chemistry and no ML** — it only:

1. Builds a readable *scenario manifest* from ``phreeqc_results.csv`` (converting
   the molality columns PHREEQC already produced to mM and inferring a little
   metadata from the source filename, marking anything it cannot infer as
   ``unknown`` rather than guessing), and
2. Scores each scenario against a measured sample with a transparent rule-based
   score (no learned weights) so the app can *suggest* the best matches and flag
   samples that need a brand-new PHREEQC simulation.

The molality→mM conversion is the same one ``compare/residuals.py`` already uses
(``config.PHREEQC_MOLALITY_TO_MM``); nothing here recomputes PHREEQC output.
"""
from __future__ import annotations

import functools
import math
import re
from pathlib import Path

import pandas as pd

from . import config

# --------------------------------------------------------------------------- #
# Manifest schema
# --------------------------------------------------------------------------- #
MANIFEST_COLUMNS = [
    "phreeqc_record_key",
    "source_file",
    "simulation",
    "state",
    "solution_number",
    "predicted_pH",
    "predicted_Ca_mM",
    "predicted_Si_mM",
    "predicted_Al_mM",
    "predicted_Fe_mM",
    "liquid_solid_ratio",
    "CO2_condition",
    "NaOH_M",
    "time_min",
    "temperature_C",
    "scenario_label",
    "metadata_quality",
    "notes",
    # Generated-scenario tags (Prompt 11) — empty/False for hand-built rows.
    "generated",
    "source_condition_key",
    "generated_at",
]

UNKNOWN = "unknown"

# Molality columns PHREEQC reports -> the measured mM column they correspond to.
_MOLALITY_TO_MM = {
    "Ca": "mol_Ca",
    "Si": "mol_Si",
    "Al": "mol_Al",
    "Fe": "mol_Fe",
}

# CO2 vocabulary families. Two labels are "compatible" if they share a family
# (or either is unknown); opposite families are a real conflict, not just a missing
# match. Two families:
#   atmospheric — open air to atmospheric CO2: OA (cup cover), atm_CO2 (model).
#   reduced     — reduced CO2 exchange: PF/GS (cup covers, *not* confirmed airtight),
#                 low_CO2 / no_CO2 (model). NOTE: never called "sealed".
# Legacy labels (open / sealed) are still recognised so old data still classifies.
ATMOSPHERIC = "atmospheric"
REDUCED = "reduced"
_CO2_ATMOSPHERIC = {"oa", "atm_co2", "atmco2", "atmospheric", "atmospheric/open", "open"}
_CO2_REDUCED = {"pf", "gs", "low_co2", "lowco2", "no_co2", "noco2", "sealed", "sealed-like"}

# Confidence bands for the rule-based score. Max achievable = the positive weights
# (batch +3, L/S +3, CO2 +2, temperature +1).
HIGH_SCORE = 7
MEDIUM_SCORE = 4  # below this is "low" -> treated as no good match
MAX_SCORE = 9


# --------------------------------------------------------------------------- #
# Small parsing helpers
# --------------------------------------------------------------------------- #
def _to_float(value) -> float | None:
    """Best-effort float; returns None for blanks/NaN/non-numeric."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "unknown", "none"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _truthy(value) -> bool:
    """True for real truthy values incl. the strings PHREEQC results CSVs round-trip."""
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def co2_family(label) -> str:
    """Normalise a CO2 label to a family: ``atmospheric`` / ``reduced`` / ``unknown``.

    ``OA`` (open air) and ``atm_CO2`` are *atmospheric*; the cup covers ``PF``/``GS``
    and the model labels ``low_CO2``/``no_CO2`` are *reduced*. PF/GS are reduced but
    not confirmed airtight, so this family is never named "sealed".
    """
    if label is None:
        return UNKNOWN
    token = str(label).strip().lower()
    if not token or token == "nan":
        return UNKNOWN
    if token in _CO2_ATMOSPHERIC:
        return ATMOSPHERIC
    if token in _CO2_REDUCED:
        return REDUCED
    return UNKNOWN


def co2_compatible(sample_label, scenario_label) -> bool:
    """True if two CO2 labels share a family (or either is unknown).

    Compatibility (no hard conflict) is not the same as an *exact* CO2 match:

    * **OA ↔ atmospheric model (atm_CO2)** is a genuine match — the open-air cover is
      directly represented by an atmospheric-CO2 scenario, so OA can reach *exact*.
    * **PF/GS ↔ reduced model (low_CO2/no_CO2)** share the *reduced* family and so are
      compatible, but the match is **unconfirmed** — PF/GS are not confirmed airtight
      and the model does not represent that specific cover. Such a same-family match
      must NOT count as exact for the CO2 field; :func:`_metadata_alignment` caps a
      PF/GS sample to *scenario-level only* until a model scenario explicitly carries
      that PF/GS cover code (see :func:`replicates.mapping_status`).
    """
    a, b = co2_family(sample_label), co2_family(scenario_label)
    return a == UNKNOWN or b == UNKNOWN or a == b


# --------------------------------------------------------------------------- #
# Feature 1 — metadata inference + scenario manifest
# --------------------------------------------------------------------------- #
def infer_metadata_from_filename(source_file: str) -> dict:
    """Infer the little metadata a PHREEQC filename safely reveals.

    Only confident tokens are used; everything else stays ``unknown``:
    * ``L-S_5`` (or ``LS5``) -> ``liquid_solid_ratio = 5``
    * ``atmCO2`` -> ``CO2_condition = atm_CO2`` (atmospheric family)
    * ``lowCO2`` -> ``CO2_condition = low_CO2`` (reduced family)
    * ``noCO2``  -> ``CO2_condition = no_CO2`` (no CO2 ingress; reduced family)
    """
    name = str(source_file or "")
    out = {"liquid_solid_ratio": None, "CO2_condition": UNKNOWN, "notes": []}

    m = re.search(r"L[-_ ]?S[-_ ]?(\d+(?:\.\d+)?)", name, flags=re.IGNORECASE)
    if m:
        out["liquid_solid_ratio"] = float(m.group(1))
        out["notes"].append(f"L/S {m.group(1)} inferred from filename")

    low = name.lower()
    if "atmco2" in low:
        out["CO2_condition"] = "atm_CO2"
        out["notes"].append("atmCO2 -> atm_CO2 (atmospheric)")
    elif "lowco2" in low:
        out["CO2_condition"] = "low_CO2"
        out["notes"].append("lowCO2 -> low_CO2 (reduced)")
    elif "noco2" in low:
        out["CO2_condition"] = "no_CO2"
        out["notes"].append("noCO2 -> no_CO2 (no CO2 ingress; reduced)")

    return out


def _metadata_quality(ls_ratio, co2_condition) -> str:
    """How much filename metadata we could infer: good / partial / unknown."""
    have_ls = ls_ratio is not None
    have_co2 = co2_condition not in (None, UNKNOWN)
    if have_ls and have_co2:
        return "good"
    if have_ls or have_co2:
        return "partial"
    return UNKNOWN


def _scenario_label(stem: str, state, ls_ratio, co2_condition, solution_number) -> str:
    """Human-readable one-line label for a PHREEQC scenario."""
    parts: list[str] = []
    if ls_ratio is not None:
        parts.append(f"L/S {ls_ratio:g}")
    if co2_condition not in (None, UNKNOWN):
        parts.append(str(co2_condition))
    state_txt = str(state) if state not in (None, "") else "?"
    sol = "" if solution_number in (None, "") else f" sol{solution_number}"
    cond = ", ".join(parts) if parts else stem
    return f"{cond} — {state_txt}{sol}"


def build_scenario_manifest(results: pd.DataFrame) -> pd.DataFrame:
    """Build the scenario manifest from a parsed ``phreeqc_results`` frame.

    Missing molality columns (e.g. ``mol_Fe``, which CEMDATA18 often omits) become
    NaN predictions, not zero — "unavailable", not "predicted zero".
    """
    if results is None or results.empty:
        return pd.DataFrame(columns=MANIFEST_COLUMNS)

    rows: list[dict] = []
    for _, r in results.iterrows():
        source_file = r.get("source_file", "")
        inferred = infer_metadata_from_filename(source_file)
        ls_ratio = inferred["liquid_solid_ratio"]
        co2 = inferred["CO2_condition"]
        state = r.get("state", "")
        stem = str(source_file).rsplit(".", 1)[0]

        # Generated scenarios (Prompt 11) carry their exact condition metadata, so
        # the manifest uses that instead of filename inference — this is what lets an
        # OA condition reach an *exact* mapping (time_min + NaOH_M are now present).
        gen = _truthy(r.get(config.GENERATED_FLAG_COLUMN))
        gen_naoh = gen_time = float("nan")
        if gen:
            _g = config.GENERATED_META_COLUMNS
            _ls = _to_float(r.get(_g["liquid_solid_ratio"]))
            if _ls is not None:
                ls_ratio = _ls
            _co2 = r.get(_g["CO2_condition"])
            if _co2 not in (None, "") and not (isinstance(_co2, float) and pd.isna(_co2)):
                co2 = str(_co2)
            gen_naoh = _to_float(r.get(_g["NaOH_M"]))
            gen_time = _to_float(r.get(_g["time_min"]))

        predicted = {}
        for element, mol_col in _MOLALITY_TO_MM.items():
            mol = _to_float(r.get(mol_col)) if mol_col in results.columns else None
            predicted[f"predicted_{element}_mM"] = (
                mol * config.PHREEQC_MOLALITY_TO_MM if mol is not None else float("nan")
            )

        notes = list(inferred["notes"])
        if "mol_Fe" not in results.columns:
            notes.append("Fe not predicted by PHREEQC (unavailable, not zero)")
        if str(state).lower() == "initial":
            notes.append("initial = starting solution, usually not the final measured state")

        rows.append({
            "phreeqc_record_key": r.get("record_key", ""),
            "source_file": source_file,
            "simulation": r.get("simulation", ""),
            "state": state,
            "solution_number": r.get("solution_number", ""),
            "predicted_pH": _to_float(r.get("pH")),
            **predicted,
            "liquid_solid_ratio": ls_ratio if ls_ratio is not None else float("nan"),
            "CO2_condition": co2,
            # NaOH_M / time_min are not in the hand-built PHREEQC filenames, so they
            # stay NaN there; generated scenarios fill them from the source condition.
            "NaOH_M": gen_naoh,
            "time_min": gen_time,
            "temperature_C": (_to_float(r.get(config.GENERATED_META_COLUMNS["temperature_C"]))
                              if gen else _to_float(r.get("temperature_c"))),
            "scenario_label": _scenario_label(
                stem, state, ls_ratio, co2, r.get("solution_number", "")),
            "metadata_quality": _metadata_quality(ls_ratio, co2),
            "notes": "; ".join(notes),
            "generated": bool(gen),
            "source_condition_key": (r.get(config.GENERATED_SOURCE_COLUMN, "") if gen else ""),
            "generated_at": (r.get(config.GENERATED_AT_COLUMN, "") if gen else ""),
        })

    return pd.DataFrame(rows, columns=MANIFEST_COLUMNS)


def scenario_manifest_path() -> Path:
    """Path the manifest CSV is written to (under ``data/processed/``)."""
    return config.PROCESSED_DIR / config.PHREEQC_SCENARIO_MANIFEST_CSV


def load_results(results_path: Path | None = None) -> pd.DataFrame:
    """Read ``phreeqc_results.csv`` (empty frame if it does not exist yet)."""
    path = results_path or (config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV)
    if not Path(path).exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def write_scenario_manifest(results_path: Path | None = None,
                            out_path: Path | None = None) -> Path:
    """Build the manifest from results and write it to ``data/processed/``."""
    manifest = build_scenario_manifest(load_results(results_path))
    dest = out_path or scenario_manifest_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(dest, index=False)
    return dest


# --------------------------------------------------------------------------- #
# Solution descriptions — explain what sol1 / sol2 / sol3 represent
# --------------------------------------------------------------------------- #
SOLUTION_DESCRIPTION_COLUMNS = [
    "source_file", "solution_number", "solution_label", "ph", "temp", "description",
]


def describe_solutions(input_solutions: pd.DataFrame) -> pd.DataFrame:
    """One readable row per PHREEQC input solution (from parsed ``.pqi`` data).

    Takes the long ``phreeqc_input_solutions`` frame (the ``pqi_parser`` output:
    ``source_file, solution_number, solution_label, temp, ph, …``) and collapses it
    to one row per ``(source_file, solution_number)`` with the label PHREEQC's input
    actually gave that solution. These labels are generic (e.g. ``L-S solution 1``)
    and are **not** time points or replicates unless the input defines them so —
    that caveat is surfaced in the UI.
    """
    cols = SOLUTION_DESCRIPTION_COLUMNS
    if input_solutions is None or input_solutions.empty:
        return pd.DataFrame(columns=cols)
    if "solution_number" not in input_solutions.columns:
        return pd.DataFrame(columns=cols)

    keep = [c for c in ["source_file", "solution_number", "solution_label", "ph", "temp"]
            if c in input_solutions.columns]
    uniq = input_solutions[keep].drop_duplicates(
        subset=[c for c in ["source_file", "solution_number"] if c in keep]
    ).reset_index(drop=True)

    rows: list[dict] = []
    for _, r in uniq.iterrows():
        label = r.get("solution_label")
        label = "" if (label is None or (isinstance(label, float) and math.isnan(label))) else str(label).strip()
        num = r.get("solution_number", "")
        desc = label if label else f"PHREEQC solution {num} (no label in the input file)"
        rows.append({
            "source_file": r.get("source_file", ""),
            "solution_number": num,
            "solution_label": label,
            "ph": _to_float(r.get("ph")),
            "temp": _to_float(r.get("temp")),
            "description": desc,
        })
    return pd.DataFrame(rows, columns=cols)


def load_solution_descriptions(input_solutions_path: Path | None = None) -> pd.DataFrame:
    """Read ``phreeqc_input_solutions.csv`` and describe each solution.

    Empty frame (with the right columns) if the parsed-input CSV does not exist yet.
    """
    path = input_solutions_path or (config.PROCESSED_DIR / config.PHREEQC_INPUT_SOLUTIONS_CSV)
    if not Path(path).exists():
        return pd.DataFrame(columns=SOLUTION_DESCRIPTION_COLUMNS)
    return describe_solutions(pd.read_csv(path))


# --------------------------------------------------------------------------- #
# Feature 3 — rule-based scoring + suggestions
# --------------------------------------------------------------------------- #
def confidence_for(score: int) -> str:
    """Map a rule-based score to high / medium / low confidence (before caps)."""
    if score >= HIGH_SCORE:
        return "high"
    if score >= MEDIUM_SCORE:
        return "medium"
    return "low"


# Ordering so a cap can never *raise* confidence, only lower it.
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
_CONDITION_CODE_RE = re.compile(r"(?<![A-Z])(OA|PF|GS)(?![A-Z])")

# OA / PF / GS are now known cup-cover / CO2 exposure conditions.
#   OA = open air  (directly exposed to atmospheric CO2)
#   PF = plastic flap cover  (covered cup, reduced CO2 exchange — not sealed)
#   GS = glass cover         (covered cup, reduced CO2 exchange — not sealed)
# PF / GS are NOT treated as fully sealed unless airtight sealing is confirmed.
COVER_CONDITION = {
    "OA": "open_air",
    "PF": "plastic_flap",
    "GS": "glass_cover",
}
# Qualitative CO2 exposure level implied by the cover (NOT the PHREEQC open/sealed
# vocabulary). OA is open air; PF/GS are covered cups with reduced — never fully
# sealed — CO2 exchange.
CO2_EXPOSURE_LEVEL = {
    "OA": "open",
    "PF": "reduced",
    "GS": "reduced",
}


def cover_condition(code: str | None) -> str | None:
    """Human-readable cup-cover condition for an OA/PF/GS code (else ``None``)."""
    if code is None:
        return None
    return COVER_CONDITION.get(str(code).strip().upper())


def co2_exposure_level(code: str | None) -> str | None:
    """Qualitative CO2 exposure level implied by an OA/PF/GS cover (else ``None``).

    OA is open air (high atmospheric CO2); PF and GS are covered cups with reduced
    exchange. This is a *qualitative* exposure descriptor — it is deliberately not
    the open/sealed PHREEQC ``CO2_condition`` vocabulary, because PF/GS are not
    confirmed airtight.
    """
    if code is None:
        return None
    return CO2_EXPOSURE_LEVEL.get(str(code).strip().upper())


def _min_confidence(a: str, b: str) -> str:
    """The lower of two confidence labels."""
    return a if CONFIDENCE_ORDER.get(a, 0) <= CONFIDENCE_ORDER.get(b, 0) else b


@functools.lru_cache(maxsize=None)
def _code_regex(codes: tuple):
    """Compiled ``(CODE1|CODE2|…)`` regex for the given condition codes (cached)."""
    if not codes:
        return None
    return re.compile("(?<![A-Z])(" + "|".join(re.escape(c) for c in codes) + ")(?![A-Z])")


def sample_condition_code(sample: dict, profile=None) -> str | None:
    """Experimental condition code (OA/PF/GS for fly ash), if the sample carries one.

    Looks at an explicit ``condition_code`` / ``extra__condition_code`` / the profile's
    condition column first (the cup cover is the CO2_condition value for fly ash), then
    falls back to the ``sample_id`` and ``notes`` text (e.g. ``...-NaOH-OA-10min``). With
    no profile, the fly-ash codes (OA/PF/GS) and ``CO2_condition`` column are used, so
    existing callers are unchanged; a :class:`profiles.DatasetProfile` supplies a
    different code set / condition column for another dataset.
    """
    if profile is None:
        codes = ("OA", "PF", "GS")
        cols = ("condition_code", "extra__condition_code", "CO2_condition")
        regex = _CONDITION_CODE_RE
    else:
        codes = tuple(str(c).strip().upper() for c in profile.condition_codes)
        cols = ("condition_code", "extra__condition_code", profile.condition_column)
        regex = _code_regex(codes)
    for key in cols:
        v = sample.get(key)
        if v not in (None, "") and str(v).strip().upper() in codes:
            return str(v).strip().upper()
    if regex is not None:
        for key in ("sample_id", "notes"):
            m = regex.search(str(sample.get(key, "")).upper())
            if m:
                return m.group(1)
    return None


def _metadata_alignment(sample: dict, scenario: dict, profile=None) -> dict:
    """Compare *experimental* vs *PHREEQC* metadata and decide a confidence cap.

    The PHREEQC scenario manifest (built from ``phreeqc_results.csv`` + filenames)
    does not carry ``time_min``, an OA/PF/GS ``condition_code`` or ``NaOH_M`` — those
    are simply not in the PHREEQC files. So when the *experiment* specifies one of
    them but PHREEQC does not, the match cannot be confirmed exactly: confidence is
    capped at **medium**. High confidence therefore requires the experiment and
    PHREEQC to actually align on these, not just share L/S + CO2 + batch state.
    """
    notes: list[str] = []
    phreeqc_missing: list[str] = []
    trace: list[dict] = []
    cap = "high"

    def _cap_entry(field, sample_value, scenario_value, note):
        """Record a metadata-quality cap: caps confidence to medium, no score change."""
        notes.append(note)
        phreeqc_missing.append(field)
        trace.append({
            "field": field, "sample_value": sample_value, "scenario_value": scenario_value,
            "outcome": "missing", "points": 0,
            "note": note + " Caps confidence to medium (no score change).",
        })

    exp_time = _to_float(sample.get("time_min"))
    pheq_time = _to_float(scenario.get("time_min"))
    if exp_time is not None and pheq_time is None:
        cap = "medium"
        _cap_entry("time_min", exp_time, None,
                   "Experimental time is known, but the selected PHREEQC scenario does not specify time.")

    exp_code = sample_condition_code(sample, profile)
    pheq_code_raw = scenario.get("condition_code")
    pheq_code = (str(pheq_code_raw).strip().upper()
                 if pheq_code_raw not in (None, "") else None)
    # PF/GS are reduced-CO2 cup covers that are NOT confirmed airtight: a same-family
    # ("reduced") model match is *unconfirmed*, so cap to medium until a model scenario
    # explicitly carries that cover code. OA (open air) is directly represented by an
    # atmospheric-CO2 model scenario, so it is NOT capped here — a real CO2 mismatch for
    # OA (e.g. OA vs a reduced scenario) is caught as a family conflict in
    # replicates.mapping_status, and OA vs an atmospheric scenario can reach exact.
    if exp_code in ("PF", "GS") and exp_code != pheq_code:
        cap = "medium"
        _cap_entry("condition_code", exp_code, pheq_code,
                   f"Experimental cup-cover {exp_code} ({cover_condition(exp_code)}) is a reduced-CO2 "
                   "cover the model does not explicitly represent. PF/GS are not confirmed airtight, so "
                   "this is at best a scenario-level (reduced-CO2 family) match, not an exact CO2 match.")

    exp_naoh = _to_float(sample.get("NaOH_M"))
    pheq_naoh = _to_float(scenario.get("NaOH_M"))
    if exp_naoh is not None and pheq_naoh is None:
        cap = "medium"
        _cap_entry("NaOH_M", exp_naoh, None,
                   "Experimental NaOH molarity is known, but the PHREEQC scenario does not specify NaOH_M.")

    experimental_known = {
        "NaOH_M": exp_naoh is not None,
        "time_min": exp_time is not None,
        "condition_code": exp_code is not None,
        "liquid_solid_ratio": _to_float(sample.get("liquid_solid_ratio")) is not None,
        "CO2_condition": co2_family(sample.get("CO2_condition")) != UNKNOWN,
        "temperature_C": _to_float(sample.get("temperature_C")) is not None,
    }
    return {
        "cap": cap,
        "metadata_notes": notes,
        "experimental_known": experimental_known,
        "phreeqc_missing": phreeqc_missing,
        "experimental_condition_code": exp_code,
        "trace": trace,
    }


# Leachant family normalization tokens (case/synonym-insensitive).
_ACID_TOKENS = ("hcl", "acid", "hno3", "h2so4", "nitric", "sulfuric", "hydrochloric")


def _leachant_family(leachant) -> str | None:
    """Group a leachant label into an ``acid`` / ``base`` family (``None`` if blank)."""
    s = str(leachant or "").strip().lower()
    if not s or s == "nan":
        return None
    return "acid" if any(t in s for t in _ACID_TOKENS) else "base"


# Outcome vocabulary for a trace entry.
TRACE_OUTCOMES = ("matched", "missing", "conflict", "normalized")


def _collect_trace_fields(trace: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Pull the human field labels back out of the trace (one place, no re-derivation)."""
    matched = [e["label"] for e in trace if e["outcome"] == "matched" and e.get("label")]
    conflict = [e["label"] for e in trace if e["outcome"] == "conflict" and e.get("label")]
    missing = [e["label"] for e in trace if e["outcome"] == "missing" and e.get("label")]
    return matched, conflict, missing


def reason_from_trace(trace: list[dict]) -> str:
    """Assemble the flat reason string FROM the trace (the single source for it)."""
    matched, conflict, _ = _collect_trace_fields(trace)
    if conflict:
        return "; ".join(matched + [f"CONFLICT: {c}" for c in conflict])
    if matched:
        return "; ".join(matched)
    return "no comparable metadata"


def confidence_explanation(score: int, base_confidence: str, final_confidence: str,
                           phreeqc_missing: list[str]) -> str:
    """Human banding math: ``score X of max 9 → base; capped to final because …``."""
    text = f"score {score} of max {MAX_SCORE} → {base_confidence}"
    if final_confidence != base_confidence:
        why = ", ".join(phreeqc_missing) if phreeqc_missing else \
            "experimental metadata the model cannot confirm"
        text += f"; capped to {final_confidence} because the model does not specify {why}"
    return text


def score_scenario(sample: dict, scenario: dict, profile=None) -> dict:
    """Rule-based score for mapping ``sample`` to a PHREEQC ``scenario`` row.

    Transparent, hand-written rules (no learned weights):
    * +3 batch state, -4 initial state,
    * +3 matching liquid_solid_ratio,
    * +2 compatible CO2_condition family,
    * +1 matching-or-unknown temperature,
    * -2 on a major conflict (opposite CO2 family or a different known L/S).

    Returns a machine-readable ``trace`` (one entry per rule that fired, each
    ``{field, sample_value, scenario_value, outcome, points, note}``) from which the
    flat ``reason``, the ``matched_fields`` / ``mismatched_fields`` / ``missing_metadata``
    lists, the ``score_breakdown`` and the ``confidence_explanation`` are all derived —
    a single source so the UI explanation is *generated*, not re-derived. The integer
    ``score`` always equals the sum of the trace entries' ``points``.
    """
    trace: list[dict] = []
    score = 0

    def record(field, sample_value, scenario_value, outcome, points, note, label=None):
        nonlocal score
        score += points
        entry = {"field": field, "sample_value": sample_value,
                 "scenario_value": scenario_value, "outcome": outcome,
                 "points": int(points), "note": note}
        if label is not None:
            entry["label"] = label  # the human token reused for reason / *_fields
        trace.append(entry)

    # 0) Leachant family normalization — informational (not scored here; the acid/NaOH
    #    safety decision lives in replicates.mapping_status).
    fam = _leachant_family(sample.get("leachant"))
    if fam:
        record("leachant", sample.get("leachant"), None, "normalized", 0,
               f"Leachant '{sample.get('leachant')}' grouped as {fam} family "
               "(case/synonym-normalized).")

    # 1) PHREEQC state.
    state_raw = scenario.get("state", "")
    state = str(state_raw).strip().lower()
    if state == "batch":
        record("state", None, state_raw, "matched", 3,
               "state = batch (post-equilibration)", label="state=batch")
    elif state == "initial":
        record("state", None, state_raw, "conflict", -4,
               "state = initial (starting solution)",
               label="state=initial (starting solution)")

    # 2) Liquid/solid ratio.
    s_ls = _to_float(sample.get("liquid_solid_ratio"))
    m_ls = _to_float(scenario.get("liquid_solid_ratio"))
    if s_ls is None or m_ls is None:
        record("liquid_solid_ratio", s_ls, m_ls, "missing", 0,
               "liquid_solid_ratio not comparable (one side unknown)",
               label="liquid_solid_ratio")
    elif abs(s_ls - m_ls) < 1e-6:
        record("liquid_solid_ratio", s_ls, m_ls, "matched", 3,
               "liquid_solid_ratio matches", label="liquid_solid_ratio")
    else:
        record("liquid_solid_ratio", s_ls, m_ls, "conflict", 0,
               f"liquid_solid_ratio differs ({s_ls:g} vs {m_ls:g})",
               label=f"liquid_solid_ratio ({s_ls:g} vs {m_ls:g})")

    # 3) CO2 condition — fuzzy family grouping, then the family-match score.
    s_co2 = sample.get("CO2_condition")
    m_co2 = scenario.get("CO2_condition")
    sf, mf = co2_family(s_co2), co2_family(m_co2)
    record("CO2_condition", s_co2, m_co2, "normalized", 0,
           f"CO2 grouped by family: '{s_co2}'→{sf}, '{m_co2}'→{mf}.")
    if sf == UNKNOWN or mf == UNKNOWN:
        record("CO2_condition", s_co2, m_co2, "missing", 0,
               "CO2_condition not comparable (one side unknown family)",
               label="CO2_condition")
    elif sf == mf:
        record("CO2_condition", s_co2, m_co2, "matched", 2,
               "CO2_condition family compatible", label="CO2_condition")
    else:
        record("CO2_condition", s_co2, m_co2, "conflict", 0,
               f"CO2_condition family conflict ({sf} vs {mf})",
               label=f"CO2_condition ({s_co2} vs {m_co2})")

    # 4) Temperature.
    s_t = _to_float(sample.get("temperature_C"))
    m_t = _to_float(scenario.get("temperature_C"))
    if s_t is None or m_t is None:
        record("temperature_C", s_t, m_t, "matched", 1,
               "temperature (unknown — no penalty)", label="temperature (unknown ok)")
    elif abs(s_t - m_t) < 1.0:
        record("temperature_C", s_t, m_t, "matched", 1,
               "temperature matches", label="temperature")
    else:
        record("temperature_C", s_t, m_t, "conflict", 0,
               f"temperature differs ({s_t:g} vs {m_t:g})",
               label=f"temperature ({s_t:g} vs {m_t:g})")

    # 5) Major metadata conflict: opposite CO2 families, or two known but different L/S.
    conflict = (sf != UNKNOWN and mf != UNKNOWN and sf != mf) or (
        s_ls is not None and m_ls is not None and abs(s_ls - m_ls) >= 1e-6
    )
    if conflict:
        record("conflict_penalty", None, None, "conflict", -2,
               "major metadata conflict (CO2 family or L/S)")  # no label: not a reason field

    # 6) Metadata-quality caps (time / OA-PF-GS condition / NaOH_M the model can't
    #    confirm) — 0-point trace entries that lower the confidence band only.
    align = _metadata_alignment(sample, scenario, profile)
    trace.extend(align["trace"])

    # Everything below is DERIVED from the trace (single code path).
    matched, mismatched, missing = _collect_trace_fields(trace)
    reason = reason_from_trace(trace)
    score_breakdown = [{"rule": e["note"], "delta": e["points"]}
                       for e in trace if e["points"] != 0]

    base_confidence = confidence_for(int(score))
    final_confidence = _min_confidence(base_confidence, align["cap"])

    return {
        "score": int(score),
        "confidence": final_confidence,
        "base_confidence": base_confidence,
        "confidence_explanation": confidence_explanation(
            int(score), base_confidence, final_confidence, align["phreeqc_missing"]),
        "reason": reason,
        "matched_fields": matched,
        "mismatched_fields": mismatched,
        "missing_metadata": missing,
        "trace": trace,
        "score_breakdown": score_breakdown,
        "metadata_notes": align["metadata_notes"],
        "experimental_known": align["experimental_known"],
        "phreeqc_missing": align["phreeqc_missing"],
    }


def suggest_mappings(sample: dict, manifest: pd.DataFrame, top_n: int = 3,
                     profile=None) -> list[dict]:
    """Score every scenario for ``sample`` and return the top ``top_n`` suggestions."""
    if manifest is None or manifest.empty:
        return []
    scored: list[dict] = []
    for _, scenario in manifest.iterrows():
        result = score_scenario(sample, scenario.to_dict(), profile)
        scored.append({
            "suggested_phreeqc_record_key": scenario.get("phreeqc_record_key", ""),
            "scenario_label": scenario.get("scenario_label", ""),
            **result,
        })
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:top_n]


def best_confidence(sample: dict, manifest: pd.DataFrame, profile=None) -> str:
    """Confidence of the single best scenario for a sample (``low`` if none)."""
    top = suggest_mappings(sample, manifest, top_n=1, profile=profile)
    return top[0]["confidence"] if top else "low"


# --------------------------------------------------------------------------- #
# Feature 6 — samples that need a brand-new PHREEQC simulation
# --------------------------------------------------------------------------- #
_SIM_NEEDED_COLUMNS = [
    "sample_id",
    "NaOH_M",
    "time_min",
    "liquid_solid_ratio",
    "CO2_condition",
    "reason_new_simulation_needed",
]


def samples_needing_simulation(samples: pd.DataFrame, mapping: pd.DataFrame,
                               manifest: pd.DataFrame) -> pd.DataFrame:
    """List samples whose comparison would be weak without a new simulation.

    A sample is flagged when any of these hold:
    * it has no mapping yet,
    * its best available scenario is only *low* confidence, or
    * it shares its mapped PHREEQC row with other samples (collision -> a scatter
      plot collapses to a vertical line).
    """
    if samples is None or samples.empty or "sample_id" not in samples.columns:
        return pd.DataFrame(columns=_SIM_NEEDED_COLUMNS)

    # sample_id -> mapped record_key, and which keys are shared by 2+ samples.
    mapped: dict[str, str] = {}
    if mapping is not None and not mapping.empty and "sample_id" in mapping.columns:
        for _, m in mapping.iterrows():
            sid = str(m.get("sample_id", "")).strip()
            key = str(m.get("phreeqc_record_key", "")).strip()
            if sid and key and key.lower() != "nan":
                mapped[sid] = key
    key_counts: dict[str, int] = {}
    for key in mapped.values():
        key_counts[key] = key_counts.get(key, 0) + 1
    collided = {k for k, n in key_counts.items() if n > 1}

    rows: list[dict] = []
    for _, sample in samples.iterrows():
        sid = str(sample.get("sample_id", "")).strip()
        if not sid or sid.lower() == "nan":
            continue
        reasons: list[str] = []
        if sid not in mapped:
            reasons.append("no mapping exists")
        else:
            if mapped[sid] in collided:
                reasons.append("shares one PHREEQC row with other samples")
        if best_confidence(sample.to_dict(), manifest) == "low":
            reasons.append("best PHREEQC match is low confidence")

        if reasons:
            rows.append({
                "sample_id": sid,
                "NaOH_M": sample.get("NaOH_M", ""),
                "time_min": sample.get("time_min", ""),
                "liquid_solid_ratio": sample.get("liquid_solid_ratio", ""),
                "CO2_condition": sample.get("CO2_condition", ""),
                "reason_new_simulation_needed": "; ".join(reasons),
            })

    return pd.DataFrame(rows, columns=_SIM_NEEDED_COLUMNS)
