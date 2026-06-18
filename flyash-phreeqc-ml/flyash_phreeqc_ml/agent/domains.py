"""Materials-research **domain classification** (pure, deterministic, no AI).

The agent must decide *what kind of experiment* the user is describing, because only some
domains have an executable simulation engine. This module is a small, rule-based classifier
with a single authority for:

* the broad domain vocabulary (leaching / cementitious / red-mud / polymer / thermal /
  mechanical / corrosion / battery / unknown),
* which domains have an **executable engine** today (only leaching-geochemistry → PHREEQC),
  and which are **planning-only**, and
* the honest "no executable engine yet for this domain" message.

The LLM may *hint* a domain, but the hint is clamped to this vocabulary and a leaching
framing always wins — so a "cementitious binder" or "red mud" experiment *described as
leaching* is routed to the PHREEQC engine, while a plastic-composite *strength* test is
planning-only no matter what the model says. This keeps engine selection deterministic.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Domain vocabulary
# --------------------------------------------------------------------------- #
LEACHING_GEOCHEMISTRY = "leaching_geochemistry"
CEMENTITIOUS_BINDER = "cementitious_binder"
RED_MUD_UPCYCLING = "red_mud_upcycling"
POLYMER_COMPOSITE = "polymer_composite"
THERMAL_TREATMENT = "thermal_treatment"
MECHANICAL_TESTING = "mechanical_testing"
CORROSION_DURABILITY = "corrosion_durability"
BATTERY_MATERIAL = "battery_material"
UNKNOWN = "unknown"

DOMAINS = (
    LEACHING_GEOCHEMISTRY, CEMENTITIOUS_BINDER, RED_MUD_UPCYCLING, POLYMER_COMPOSITE,
    THERMAL_TREATMENT, MECHANICAL_TESTING, CORROSION_DURABILITY, BATTERY_MATERIAL, UNKNOWN,
)

DOMAIN_LABELS = {
    LEACHING_GEOCHEMISTRY: "leaching / geochemistry",
    CEMENTITIOUS_BINDER: "cementitious binder",
    RED_MUD_UPCYCLING: "red mud upcycling",
    POLYMER_COMPOSITE: "polymer / plastic composite",
    THERMAL_TREATMENT: "thermal treatment",
    MECHANICAL_TESTING: "mechanical testing",
    CORROSION_DURABILITY: "corrosion / durability",
    BATTERY_MATERIAL: "battery material",
    UNKNOWN: "unknown",
}

# --------------------------------------------------------------------------- #
# Engines
# --------------------------------------------------------------------------- #
ENGINE_PHREEQC = "phreeqc"

# The single executable engine today. Everything else is planning-only.
EXECUTABLE_DOMAINS = {LEACHING_GEOCHEMISTRY: ENGINE_PHREEQC}

PLANNING_ONLY_DOMAINS = (
    CEMENTITIOUS_BINDER, RED_MUD_UPCYCLING, POLYMER_COMPOSITE, THERMAL_TREATMENT,
    MECHANICAL_TESTING, CORROSION_DURABILITY, BATTERY_MATERIAL, UNKNOWN,
)

ENGINE_NOTE = (
    "The PHREEQC engine is currently the only executable engine, and it applies to "
    "leaching / geochemical (aqueous dissolution) scenarios. Other domains are supported for "
    "planning only — there is no executable simulation engine for them yet.")

# Engines/capabilities that could be added modularly (shown as "future", never implied to exist).
FUTURE_ENGINES = (
    "Literature retrieval (RAG) for sourced benchmarks",
    "Surrogate ML models (fast approximations of a simulator)",
    "Atomistic / molecular simulation engines",
    "Mechanical-property / strength-prediction models",
)

# Domains a trained **ML surrogate** (mechanical-property prediction) can serve. The surrogate is
# NOT an executable simulation engine (it predicts from data); it never reaches PHREEQC. Whether a
# trained model actually exists is runtime state — the UI checks the model registry and passes a
# flag; this module only knows which domains the surrogate is *relevant* to.
ML_SURROGATE_DOMAINS = (POLYMER_COMPOSITE, MECHANICAL_TESTING, CEMENTITIOUS_BINDER)
ENGINE_ML_SURROGATE = "ml_surrogate"
# Stable marker so the orchestrator can de-dupe the ML offer in an assembled message.
ML_SURROGATE_MARKER = "trained ML surrogate model is available"


def supports_ml_surrogate(domain: str) -> bool:
    """True when a (mechanical-property) ML surrogate is *relevant* to ``domain``.

    This says nothing about whether a model has actually been trained — that is checked at the UI
    via the model registry and passed in as a flag.
    """
    return domain in ML_SURROGATE_DOMAINS


def ml_surrogate_offer(domain: str) -> str:
    """One-line offer to use a trained ML surrogate for ``domain`` (predictions are experimental).

    Only shown when a model genuinely exists (the caller checks the registry). PHREEQC is never the
    strength engine — this is a separate, data-trained prediction engine, and its output is an
    experimental estimate with uncertainty, not a validated value or a measurement."""
    return (f"✅ A {ML_SURROGATE_MARKER} for **{label(domain)}** — I can give an **experimental "
            "(not validated)** estimate with an uncertainty range in **Prediction Models** "
            "(PHREEQC is the leaching engine, not the strength engine; the ML surrogate is the "
            "prediction engine here). It is a screening estimate, not a measurement.")

# --------------------------------------------------------------------------- #
# Planning support for the (currently) non-executable domains.
# For each, what a researcher typically measures (response variables) and what a future model
# would need as inputs — so the assistant can structure the experiment + build a data template
# instead of dead-ending. This is PLANNING ONLY; it never simulates.
# --------------------------------------------------------------------------- #
PLANNING_DOMAIN_INFO = {
    POLYMER_COMPOSITE: {
        "outcome": "mechanical strength",
        "response_variables": ["compressive strength", "flexural strength", "density",
                               "water absorption", "toughness"],
        "input_variables": ["plastic type (e.g. PET / HDPE / PP)", "plastic form (fiber / flake / pellet)",
                            "plastic size", "fly ash : plastic ratio", "binder / activator composition",
                            "water / binder ratio", "curing time + condition", "specimen geometry",
                            "test standard", "measured strength / density / absorption values"],
        "future_engine": "a strength / mechanical-property prediction model (empirical or ML)",
    },
    MECHANICAL_TESTING: {
        "outcome": "mechanical performance",
        "response_variables": ["compressive strength", "flexural strength", "tensile strength",
                               "elastic modulus", "hardness", "toughness"],
        "input_variables": ["material composition", "specimen geometry", "loading rate",
                            "curing/conditioning", "test standard", "measured strength values"],
        "future_engine": "a mechanical-property prediction model (empirical or ML)",
    },
    THERMAL_TREATMENT: {
        "outcome": "thermal transformation",
        "response_variables": ["phase changes", "mass loss", "crystallinity",
                               "specific surface area", "calcination yield", "reactivity"],
        "input_variables": ["atmosphere", "ramp rate", "dwell time", "peak temperature",
                            "starting composition", "particle size",
                            "measurements (XRD / FTIR / TGA)"],
        "future_engine": "a thermodynamic / phase-evolution (calcination) model",
    },
    CEMENTITIOUS_BINDER: {
        "outcome": "binder performance",
        "response_variables": ["compressive strength", "setting time", "workability",
                               "heat of hydration", "porosity"],
        "input_variables": ["binder ratios", "activator dose", "water/binder ratio",
                            "curing regime", "precursor composition", "measured strength values"],
        "future_engine": "a hydration / strength-development model",
    },
    BATTERY_MATERIAL: {
        "outcome": "electrochemical performance",
        "response_variables": ["specific capacity", "cycling stability", "coulombic efficiency",
                               "rate capability"],
        "input_variables": ["active material", "electrode composition", "electrolyte",
                            "mass loading", "C-rate", "measured capacity values"],
        "future_engine": "an electrochemical performance model",
    },
    CORROSION_DURABILITY: {
        "outcome": "durability",
        "response_variables": ["corrosion rate", "chloride ingress depth", "carbonation depth",
                               "mass loss"],
        "input_variables": ["exposure environment", "material composition", "cover depth",
                            "exposure time", "measured corrosion/ingress values"],
        "future_engine": "a transport / durability model",
    },
    RED_MUD_UPCYCLING: {
        "outcome": "recovery / upcycling performance",
        "response_variables": ["element recovery", "product yield", "residual composition",
                               "leachate composition"],
        "input_variables": ["process route", "reagent + dose", "temperature / time",
                            "starting assay", "measured product / leachate composition"],
        # If framed as a leaching/dissolution step, the assistant classifies it as
        # leaching_geochemistry and the PHREEQC engine becomes available (handled by classify()).
        "future_engine": "a process / recovery model",
    },
    UNKNOWN: {
        "outcome": "the property of interest",
        "response_variables": ["the measured outcome(s) you care about"],
        "input_variables": ["the controllable inputs", "the material/composition",
                            "the process conditions", "the measured outcome values"],
        "future_engine": "a domain-specific model",
    },
}

# The next actions the assistant can take for a planning-only domain (never a simulation).
PLANNING_NEXT_ACTIONS = (
    "structure an experiment plan",
    "build a data template for these variables",
    "identify the missing variables",
    "(later) add sourced literature benchmarks",
)


def planning_support(domain: str) -> dict:
    """Structured planning support for a non-executable domain (response/input vars + offers).

    Returns the :data:`PLANNING_DOMAIN_INFO` entry (falling back to the generic UNKNOWN entry)
    plus the standard next actions. Never simulates — this is planning/data-prep only.
    """
    info = dict(PLANNING_DOMAIN_INFO.get(domain, PLANNING_DOMAIN_INFO[UNKNOWN]))
    info["domain"] = domain
    info["domain_label"] = label(domain)
    info["next_actions"] = list(PLANNING_NEXT_ACTIONS)
    return info


def data_template_columns(domain: str) -> tuple:
    """``(csv_columns, human_labels)`` for a planning-only domain's data-collection template.

    Columns are ``sample_id`` + the input variables + the response variables (slugified). The
    same helper is used by the agent tool and the UI so they always agree. Planning/data only —
    not a prediction.
    """
    s = planning_support(domain)
    variables = list(s["input_variables"]) + list(s["response_variables"])
    labels = ["sample_id"] + variables
    cols = ["sample_id"] + [re.sub(r"[^0-9a-z]+", "_", str(v).lower()).strip("_") or "value"
                            for v in variables]
    return cols, labels


def engine_status() -> dict:
    """The 'engines & capabilities' panel data (available now / planning now / future)."""
    return {
        "available_now": [
            {"capability": "Leaching / dissolution / geochemistry",
             "engine": ENGINE_PHREEQC, "note": "aqueous dissolution — runs after you confirm"}],
        "planning_now": [
            {"capability": label(d),
             "outcome": PLANNING_DOMAIN_INFO.get(d, PLANNING_DOMAIN_INFO[UNKNOWN])["outcome"]}
            for d in (POLYMER_COMPOSITE, MECHANICAL_TESTING, THERMAL_TREATMENT,
                      CEMENTITIOUS_BINDER, BATTERY_MATERIAL, CORROSION_DURABILITY)],
        "future": list(FUTURE_ENGINES),
    }


# --------------------------------------------------------------------------- #
# Keyword signals (leaching framing wins; strength/thermal/etc. force planning-only)
# --------------------------------------------------------------------------- #
_LEACHING_RE = re.compile(
    r"\b(leach\w*|leachate|dissolv\w*|dissolution|extract\w*|release|releas\w*|"
    r"speciat\w*|geochem\w*|pore\s*solution|aqueous|solubility|precipitat\w*|"
    r"icp|naoh|koh|hcl|acid\b|alkal\w*|ph\b|molarit\w*|mol/l|liquid|solution)\b", re.I)

# Strong aqueous cues = the leaching cues EXCEPT a bare "pH". A bare pH is a weak signal: a
# cementitious/geopolymer *strength* study may mention pore-solution pH without wanting an
# aqueous-chemistry simulation, so pH alone must not route a strength study to PHREEQC.
_STRONG_AQUEOUS_RE = re.compile(
    r"\b(leach\w*|leachate|dissolv\w*|dissolution|extract\w*|release|releas\w*|"
    r"speciat\w*|geochem\w*|pore\s*solution|aqueous|solubility|precipitat\w*|"
    r"icp|naoh|koh|hcl|acid\b|alkal\w*|molarit\w*|mol/l|liquid|solution)\b", re.I)

_MECHANICAL_RE = re.compile(
    r"\b(compressive|flexural|tensile|strength|young'?s modulus|stiffness|"
    r"hardness|fracture|toughness|impact strength|fatigue|creep|elong\w*|"
    r"stress[-\s]?strain|load[-\s]?bearing|mpa\b|psi\b)\b", re.I)
_POLYMER_RE = re.compile(
    r"\b(polymer|plastic|composite|resin|epoxy|thermoplastic|hdpe|ldpe|pp\b|pvc|"
    r"pet\b|fibre|fiber[-\s]?reinforced|filler[-\s]?loading|matrix\s+composite)\b", re.I)
_THERMAL_RE = re.compile(
    r"\b(?:calcin\w*|sinter\w*|pyroly\w*|roast\w*|thermal treatment|heat treatment|"
    r"firing|tga|dsc|dta|furnace|kiln|anneal\w*|"
    r"phase\s+(?:change|evolution|transformation)|ramp\s*rate|dwell|xrd|ftir)\b"
    r"|\bheat\w*\b[^.]{0,40}?\b\d+\s*(?:°\s*)?c\b", re.I)
_CORROSION_RE = re.compile(
    r"\b(corros\w*|rebar|chloride ingress|carbonation depth|passivat\w*|"
    r"durability|freeze[-\s]?thaw|rebar corrosion|electrochemical impedance)\b", re.I)
_BATTERY_RE = re.compile(
    r"\b(batter\w*|cathode|anode|electrolyte|lithium[-\s]?ion|li[-\s]?ion|"
    r"capacity\s*\(?mah|cycling\s+stability|coulombic efficiency|state of charge)\b", re.I)
_CEMENT_RE = re.compile(
    r"\b(cement\w*|concrete|mortar|binder|geopolymer|alkali[-\s]?activated|"
    r"hydration|c-s-h|portland|clinker|setting time|workabilit\w*)\b", re.I)
_REDMUD_RE = re.compile(r"\b(red mud|bauxite residue|alumina refinery residue)\b", re.I)
_FLYASH_RE = re.compile(r"\b(fly ?ash|cfa\b|coal ash|class [cf] ash)\b", re.I)


def _hint_domain(hint) -> str | None:
    """Clamp a model-provided domain hint to the known vocabulary (or None)."""
    if not hint:
        return None
    h = str(hint).strip().lower().replace(" ", "_").replace("-", "_")
    return h if h in DOMAINS else None


def classify(text: str, *, hint: str | None = None) -> str:
    """Classify the experiment ``text`` into a :data:`DOMAINS` value (deterministic).

    Priority is deliberate and safety-first:

    1. A **mechanical / polymer-strength / thermal / corrosion / battery** framing forces the
       corresponding planning-only domain even if the material is fly ash / red mud — you
       cannot simulate a compressive-strength test with PHREEQC.
    2. Otherwise a **leaching / geochemistry** framing (dissolution, leachate, ICP, an acid/
       base, pH, aqueous chemistry) routes to the executable leaching domain — including a
       cementitious binder or red mud *described as leaching*.
    3. A red-mud or cementitious material with **no** leaching framing maps to its own
       planning-only domain (red-mud-upcycling / cementitious-binder).
    4. A clamped model ``hint`` breaks ties only when the rules find nothing.

    Never raises.
    """
    s = str(text or "")
    leaching = bool(_LEACHING_RE.search(s))

    # 1) Non-aqueous test framings win — never simulate these with PHREEQC. Each is gated by
    # `not leaching` so a real leaching experiment that merely mentions a token like "ionic
    # strength" or "cathode" is not hijacked away from the executable PHREEQC engine.
    if _MECHANICAL_RE.search(s) and not leaching:
        # A strength/mechanical test of a plastic/polymer composite, or anything else.
        return POLYMER_COMPOSITE if _POLYMER_RE.search(s) else MECHANICAL_TESTING
    if _POLYMER_RE.search(s) and not leaching:
        return POLYMER_COMPOSITE
    if _BATTERY_RE.search(s) and not leaching:
        return BATTERY_MATERIAL
    if _THERMAL_RE.search(s) and not leaching:
        return THERMAL_TREATMENT
    if _CORROSION_RE.search(s) and not leaching:
        return CORROSION_DURABILITY

    # 1b) A cementitious / geopolymer STRENGTH study whose only aqueous cue is a bare "pH" is
    # planning-only (cementitious_binder), NOT executable leaching — pore-solution pH alone must
    # not route a strength study to PHREEQC. A real aqueous/leaching cue (below) still wins.
    if (not _STRONG_AQUEOUS_RE.search(s) and _CEMENT_RE.search(s)
            and _MECHANICAL_RE.search(s)):
        return CEMENTITIOUS_BINDER

    # 2) Leaching / geochemistry framing → the executable domain (material-agnostic).
    if leaching:
        return LEACHING_GEOCHEMISTRY

    # 3) Material named, but no leaching framing → its planning-only domain.
    if _REDMUD_RE.search(s):
        return RED_MUD_UPCYCLING
    if _CEMENT_RE.search(s):
        return CEMENTITIOUS_BINDER
    if _FLYASH_RE.search(s):
        # Fly ash mentioned without a leaching framing — most fly-ash work in this app is
        # leaching, but without an aqueous cue we stay conservative and ask (unknown), unless
        # a hint says otherwise.
        return _hint_domain(hint) or UNKNOWN

    # 4) Fall back to a clamped model hint, else unknown.
    return _hint_domain(hint) or UNKNOWN


def is_executable(domain: str) -> bool:
    """True when ``domain`` has an executable simulation engine today."""
    return domain in EXECUTABLE_DOMAINS


def engine_for(domain: str) -> str | None:
    """The executable engine for ``domain`` (e.g. ``phreeqc``), or ``None`` (planning-only)."""
    return EXECUTABLE_DOMAINS.get(domain)


def label(domain: str) -> str:
    return DOMAIN_LABELS.get(domain, domain)


def planning_only_message(domain: str, *, ml_model_available: bool = False) -> str:
    """A useful planning-only response for a non-executable domain.

    Instead of dead-ending at "no engine", it names the domain, says honestly that no validated
    engine exists yet, offers concrete next actions (plan / data template / missing variables),
    suggests the response + input variables, and notes the future-engine path. It **never**
    implies a simulation can be run.

    When ``ml_model_available`` is True *and* the domain supports the ML surrogate, it appends an
    offer to use that trained surrogate for an **experimental (not validated)** estimate. The
    default (``False``) message is unchanged.
    """
    s = planning_support(domain)
    resp = ", ".join(s["response_variables"])
    inp = ", ".join(s["input_variables"])
    base = (
        f"This looks like a **{s['domain_label']}** problem. I don't yet have a validated "
        f"{s['outcome']} simulation engine for it and **cannot run a validated {s['outcome']} model "
        f"yet** — but I can help you **structure the experiment**, **search reliable scholarly "
        f"literature** (the **Evidence Library**), and **build an evidence / training dataset** for "
        f"a future model. Useful response variables to measure: {resp}. To model {s['outcome']} "
        f"later, we'd need: {inp}. I can {s['next_actions'][0]}, {s['next_actions'][1]}, "
        f"{s['next_actions'][2]}, search literature + curate an evidence dataset, or compare "
        f"literature ranges — just tell me which. (In future, {s['future_engine']} could be added "
        f"as a modular engine.) Note: no executable simulation runs for this domain — this is "
        "planning, literature, and data support only.")
    if ml_model_available and supports_ml_surrogate(domain):
        return base + "\n\n" + ml_surrogate_offer(domain)
    return base
