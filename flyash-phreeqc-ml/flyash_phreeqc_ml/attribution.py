"""Explain the measured mass-balance gap with PHREEQC (Prompt 24).

Prompt 22 gives a deterministic, **measured** element closure
(``gap = n_in − n_liquid − n_solid``). This module asks PHREEQC *which phases it
predicts precipitated* and computes **how much of the measured gap that accounts for** —
without ever overwriting the measured numbers. The measured closure is **immutable
input** here; everything PHREEQC says lands in clearly-separated ``modeled_*`` fields.

Filtration convention (per element; Prompt 28)
----------------------------------------------
Whether a PHREEQC-predicted precipitate reduces the gap depends on whether that
precipitate is in the **measured solid residue** (``n_solid``). For the fly-ash protocol
secondary precipitates are **retained on the filter**, so the default is *retained*:

* **retained** (``True``): the precipitate is in the assayed solid (already in
  ``n_solid``), so it explains the solid's *composition*, **not** the gap —
  ``attribution_to_gap = 0``.
* **passes** (``False``): the precipitate leaves with the filtrate and is **not** in
  ``n_solid``, so it counts toward explaining the gap — ``attribution_to_gap = min(P, gap)``.
* **uncertain**: retention is **not verified** (e.g. a colloid that may pass the filter —
  for fly ash, Si/Al/Fe). The number is computed *conservatively* as retained (credited
  ``0``), but the result is **flagged** (``filtration_uncertain``) and carries the
  alternative ``gap_explained_if_passes`` so a reviewer sees the unverified assumption.

The state is **per element** (``profiles.precipitate_in_measured_solid_for(profile, el)``):
a profile-level ``precipitate_in_measured_solid`` default plus a
``precipitate_in_measured_solid_overrides`` dict. Documented in ``docs/mass_balance.md``.

Honesty
-------
All attribution language is "model attributes" / "predicted to precipitate" — never
"the element was X". When PHREEQC cannot run (no binary / database — the same condition
the runner tests skip on), :func:`attribution_unavailable` degrades to the measured gap
with "attribution unavailable — configure PHREEQC".
"""
from __future__ import annotations

import math

import pandas as pd

from . import mass_balance, phreeqc_runner, profiles

# PHREEQC EQUI()/TOT() emit mol; the closure works in mmol.
MOL_TO_MMOL = 1000.0

PROVENANCE_MODEL = "phreeqc"
PROVENANCE_MEASURED = "measured"

# Status, paralleling the mapping-status system.
STATUS_CLOSED = "closed"                    # measured gap is within its uncertainty
STATUS_MODEL_EXPLAINED = "model-explained"  # attribution explains ~all of the gap
STATUS_PARTIAL = "partially-explained"      # attribution explains part of the gap
STATUS_UNEXPLAINED = "unexplained"          # attribution explains ~none of the gap

ATTRIBUTION_STATUSES = (STATUS_CLOSED, STATUS_MODEL_EXPLAINED, STATUS_PARTIAL,
                        STATUS_UNEXPLAINED)

# A gap is "closed" when |gap| ≤ gap_sigma (when known) or ≤ this fraction of n_in.
CLOSED_FRACTION_TOL = 0.05
EXPLAINED_TOL = 0.95   # ≥ this fraction explained → model-explained
UNEXPLAINED_TOL = 0.05  # ≤ this fraction explained → unexplained

# Per-element filtration state in an attribution result (from profiles.PRECIP_*).
FILTRATION_RETAINED = "retained"   # precipitate in the measured solid → explains the solid
FILTRATION_PASSES = "passes"       # precipitate in the filtrate → explains the gap
FILTRATION_UNCERTAIN = "uncertain"  # retention unverified (colloid may pass the filter)

# Shown (and stored in the result) whenever an element's filtration is uncertain.
_UNCERTAIN_NOTE = (
    "Filtration uncertain for {el}: retention on the filter is not verified (it may form a "
    "colloid/complex that passes), so its gap attribution rests on an unverified assumption.")


def _filtration_status(filt) -> str:
    if filt is True:
        return FILTRATION_RETAINED
    if filt is False:
        return FILTRATION_PASSES
    return FILTRATION_UNCERTAIN


# --------------------------------------------------------------------------- #
# Selected-output access
# --------------------------------------------------------------------------- #
def _selected_output_row(selected_output) -> dict:
    """Normalise a parsed selected output (DataFrame or dict) to the final-row dict."""
    if selected_output is None:
        return {}
    if isinstance(selected_output, pd.DataFrame):
        if selected_output.empty:
            return {}
        return selected_output.iloc[-1].to_dict()
    if isinstance(selected_output, dict):
        return dict(selected_output)
    raise TypeError(f"selected_output must be a DataFrame or dict, got {type(selected_output)!r}")


def _num(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _element_phases(element: str, profile) -> list[str]:
    """Candidate precipitate phases the active profile (or its material) maps to ``element``."""
    phases = profiles.candidate_phases(profile)
    return [ph for ph, el in phases.items() if str(el) == element]


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #
def _measured_block(closure: dict) -> dict:
    """The immutable measured terms (never overwritten by the model)."""
    return {
        "n_liquid": closure["n_liquid"], "n_solid": closure["n_solid"],
        "gap": closure["gap"], "gap_sigma": closure["gap_sigma"],
        "gap_fraction": closure["gap_fraction"], "closure_status": closure["status"],
    }


def _status(gap, gap_sigma, gap_fraction, fraction_explained) -> str:
    closed = ((gap_sigma is not None and abs(gap) <= gap_sigma)
              or (gap_sigma is None and gap_fraction is not None
                  and abs(gap_fraction) <= CLOSED_FRACTION_TOL))
    if closed:
        return STATUS_CLOSED
    if gap <= 0:                       # over-recovery: precipitation can't explain it
        return STATUS_UNEXPLAINED
    if fraction_explained is None:
        return STATUS_UNEXPLAINED
    if fraction_explained >= EXPLAINED_TOL:
        return STATUS_MODEL_EXPLAINED
    if fraction_explained > UNEXPLAINED_TOL:
        return STATUS_PARTIAL
    return STATUS_UNEXPLAINED


def attribute_gap(row: dict, element: str, phreeqc_selected_output, *, profile=None) -> dict:
    """Attribute the measured closure gap for ``element`` to PHREEQC-predicted phases.

    The measured closure (Prompt 22) is computed here as **immutable input**; the model
    output reduces the gap only per the ``precipitate_in_measured_solid`` convention.
    Returns ``{element, status, provenance, precipitate_in_measured_solid, measured,
    gap, modeled_precipitated_moles, by_phase, modeled_solution_moles, gap_explained,
    gap_unexplained, fraction_explained}`` — all modeled values in ``modeled_*`` /
    ``by_phase`` / ``gap_*`` fields, never in the measured block.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    closure = mass_balance.closure(row, element, profile=profile)  # immutable measured
    measured = _measured_block(closure)
    gap = closure["gap"]
    gap_sigma = closure["gap_sigma"]
    gap_fraction = closure["gap_fraction"]
    # Per-element filtration state: retained (True) / passes (False) / uncertain.
    filt = profiles.precipitate_in_measured_solid_for(profile, element)
    uncertain = (filt == profiles.PRECIP_UNCERTAIN)
    # Conservative: an *uncertain* element is treated as retained for the NUMBER (it does
    # not get to close the gap), but it is flagged so a reviewer sees the assumption.
    precip_in_solid = (filt is True) or uncertain

    so = _selected_output_row(phreeqc_selected_output)
    by_phase: dict[str, float] = {}
    for ph in _element_phases(element, profile):
        v = _num(so.get(phreeqc_runner.phase_moles_column(ph)))
        if v is not None:
            by_phase[ph] = max(v, 0.0) * MOL_TO_MMOL          # mol → mmol
    modeled_precip = sum(by_phase.values()) if by_phase else 0.0
    sol = _num(so.get(phreeqc_runner.sol_moles_column(element)))
    modeled_solution = sol * MOL_TO_MMOL if sol is not None else None

    base = {
        "element": element, "provenance": PROVENANCE_MODEL,
        "precipitate_in_measured_solid": precip_in_solid,     # effective (uncertain→True)
        "filtration_status": _filtration_status(filt),        # retained / passes / uncertain
        "filtration_uncertain": uncertain,
        "filter_cutoff_um": profiles.filter_cutoff_um(profile),
        "measured": measured,
        "gap": gap, "gap_fraction": gap_fraction,              # measured, copied read-only
        "modeled_precipitated_moles": modeled_precip, "by_phase": by_phase,
        "modeled_solution_moles": modeled_solution,
    }

    if gap is None:   # closure incomplete → nothing to attribute
        base.update(gap_explained=None, gap_unexplained=None, fraction_explained=None,
                    gap_explained_if_passes=None, status=STATUS_UNEXPLAINED)
        if uncertain:
            base["note"] = _UNCERTAIN_NOTE.format(el=element)
        return base

    # What a NON-retained precipitate could explain (the "passes" interpretation). For an
    # uncertain element this is the alternative the conservative number does NOT credit.
    explained_if_passes = min(modeled_precip, max(gap, 0.0))
    # The precipitate reduces the gap only when it is NOT in the measured solid.
    attribution_to_gap = 0.0 if precip_in_solid else explained_if_passes
    gap_explained = attribution_to_gap
    gap_unexplained = gap - gap_explained
    fraction_explained = (gap_explained / gap) if gap > 0 else None
    base.update(
        gap_explained=gap_explained, gap_unexplained=gap_unexplained,
        fraction_explained=fraction_explained,
        gap_explained_if_passes=explained_if_passes,
        status=_status(gap, gap_sigma, gap_fraction, fraction_explained))
    if uncertain:
        base["note"] = _UNCERTAIN_NOTE.format(el=element) + (
            f" Conservatively credited 0 mmol; if the {element} colloid passes the filter "
            f"the model could explain up to {explained_if_passes:.3g} mmol of the "
            f"{gap:.3g} mmol gap.")
    return base


def attribution_unavailable(row: dict, element: str, *, profile=None) -> dict:
    """Degraded result when PHREEQC cannot run — measured gap only, no modeled values.

    Status reflects the measured closure (``closed`` if the gap is within its
    uncertainty, else ``unexplained`` because nothing has been attributed yet).
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    closure = mass_balance.closure(row, element, profile=profile)
    measured = _measured_block(closure)
    gap, gap_sigma, gap_fraction = closure["gap"], closure["gap_sigma"], closure["gap_fraction"]
    if gap is not None and (
        (gap_sigma is not None and abs(gap) <= gap_sigma)
            or (gap_sigma is None and gap_fraction is not None
                and abs(gap_fraction) <= CLOSED_FRACTION_TOL)):
        status = STATUS_CLOSED
    else:
        status = STATUS_UNEXPLAINED
    filt = profiles.precipitate_in_measured_solid_for(profile, element)
    uncertain = (filt == profiles.PRECIP_UNCERTAIN)
    return {
        "element": element, "provenance": PROVENANCE_MEASURED,
        "precipitate_in_measured_solid": (filt is True) or uncertain,
        "filtration_status": _filtration_status(filt),
        "filtration_uncertain": uncertain,
        "filter_cutoff_um": profiles.filter_cutoff_um(profile),
        "measured": measured, "gap": gap, "gap_fraction": gap_fraction,
        "modeled_precipitated_moles": None, "by_phase": {},
        "modeled_solution_moles": None,
        "gap_explained": None, "gap_unexplained": gap, "fraction_explained": None,
        "gap_explained_if_passes": None,
        "status": status,
        "note": "attribution unavailable — configure PHREEQC (set PHREEQC_EXE + PHREEQC_DATABASE).",
    }


# --------------------------------------------------------------------------- #
# Honest caption + run-input builder
# --------------------------------------------------------------------------- #
def attribution_caption(result: dict) -> str:
    """Honest one-line caption — "model attributes …", never "the element was …"."""
    el = result["element"]
    if result["provenance"] != PROVENANCE_MODEL:
        return f"{el}: attribution unavailable — configure PHREEQC to explain the gap."
    if result["gap"] is None:
        return f"{el}: closure incomplete — no gap to attribute."
    phases = ", ".join(sorted(result["by_phase"])) or "no candidate phase"
    explained = result["gap_explained"] or 0.0
    if result.get("filtration_uncertain"):
        could = result.get("gap_explained_if_passes") or 0.0
        return (f"{el}: filtration uncertain — the model predicts "
                f"{result['modeled_precipitated_moles']:.3g} mmol precipitated ({phases}); "
                f"treated as retained in the measured solid (credited 0), so "
                f"{result['gap_unexplained']:.3g} mmol of the gap remain unexplained. If the "
                f"{el} colloid passes the filter it could explain up to {could:.3g} mmol — "
                "confirm by filtrate vs ultrafiltrate.")
    if result["precipitate_in_measured_solid"]:
        return (f"{el}: the model predicts {result['modeled_precipitated_moles']:.3g} mmol "
                f"precipitated ({phases}), but that is already in the measured solid — "
                f"{result['gap_unexplained']:.3g} mmol of the gap remain unexplained.")
    return (f"{el}: the model attributes {explained:.3g} of the {result['gap']:.3g} mmol "
            f"unaccounted to {phases}; {result['gap_unexplained']:.3g} mmol remain unexplained.")


def _material_inputs(row: dict, profile) -> dict:
    """Dissolved material per element as mol/L = (n_in − n_solid)/liquid_volume_mL.

    (n_in − n_solid) mmol is the element that left the solid; over the liquid volume
    that is the total dissolved before precipitation (mmol/mL = mol/L). Only complete
    closures with a known liquid volume contribute.
    """
    vol_mL = mass_balance._to_float(row.get(profile.liquid_volume_column))
    out: dict[str, float] = {}
    if vol_mL in (None, 0):
        return out
    for el in profiles.mass_balance_elements(profile):
        c = mass_balance.closure(row, el, profile=profile)
        if c["status"] == mass_balance.STATUS_COMPLETE:
            total = c["n_in"] - c["n_solid"]
            if total > 0:
                out[el] = total / vol_mL
    return out


def build_attribution_inputs(row: dict, profile=None):
    """Build the attribution ``.pqi`` variant(s) for a batch row (preview-before-run).

    Mirrors :func:`phreeqc_runner.build_input` (so OA→1 / PF-GS→2 behaviour is kept),
    threading in the dissolved material (mol/L), the profile's candidate phases, and a
    per-element SELECTED_OUTPUT. Returns the list of ``GeneratedInput`` (empty when the
    condition can't be templated or the profile declares no mass balance).
    """
    from . import config, replicates, scenarios
    profile = profile or profiles.FLY_ASH_PROFILE
    elements = list(profiles.mass_balance_elements(profile))
    if not elements or phreeqc_runner.generation_blocked_reason(row, profile):
        return []

    material_inputs = _material_inputs(row, profile)
    phases = list(profiles.candidate_phases(profile))
    naoh = phreeqc_runner._condition_naoh(row)
    ls = scenarios._to_float(row.get("liquid_solid_ratio"))
    temp = scenarios._to_float(row.get("temperature_C"))
    time_min = scenarios._to_float(row.get("time_min"))
    ph = (scenarios._to_float(row.get("final_pH"))
          or scenarios._to_float(row.get("initial_pH")))
    code = scenarios.sample_condition_code(row, profile) or "unknown"
    ckey = replicates.condition_key(row, profile)
    labels = config.COVER_TO_CO2_SCENARIOS.get(code, ["atm_CO2"])

    out = []
    for model_label in labels:
        text, assumptions = phreeqc_runner.build_single_input(
            naoh, ls if ls is not None else float("nan"), temp, model_label,
            ph=ph, time_min=time_min, label=ckey,
            material_inputs=material_inputs, candidate_phases=phases,
            selected_output_elements=elements)
        out.append(phreeqc_runner.GeneratedInput(
            model_label=model_label, condition_code=code, source_condition_key=ckey,
            pqi_text=text, assumptions=tuple(assumptions),
            metadata={"NaOH_M": naoh, "liquid_solid_ratio": ls,
                      "CO2_condition": model_label, "temperature_C": temp,
                      "time_min": time_min, "attribution": True},
            basename=f"attr_{phreeqc_runner._safe_stem(ckey)}_{model_label}"))
    return out


# --------------------------------------------------------------------------- #
# Status aggregation → validity (one source of truth in report._overall_validity)
# --------------------------------------------------------------------------- #
# Worst-first order: an unexplained gap is the most concerning.
_ATTR_WORST_ORDER = (STATUS_UNEXPLAINED, STATUS_PARTIAL, STATUS_MODEL_EXPLAINED, STATUS_CLOSED)


def overall_attribution_status(results) -> str | None:
    """The worst attribution status across results (None when there are none)."""
    present = {r["status"] for r in (results or [])}
    if not present:
        return None
    for s in _ATTR_WORST_ORDER:
        if s in present:
            return s
    return None
