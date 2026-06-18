"""Natural-language **understanding** layer for the agent (AI-first, deterministic fallback).

This is what lets the assistant cope with messy, informal, typo-filled prompts instead of
requiring exact wording. It mirrors :mod:`flyash_phreeqc_ml.ai.scenario_parser` but is
*agent-state-aware*: it returns the **delta** to merge into the conversation's running
scenario, the **changes / conflicts** versus what was already understood, the **assumptions**
the user should confirm, the **ambiguous** fields to ask about, and a compact "I understood
this as…" card for the UI.

Two paths, one entry point (:func:`extract`):

* **AI available + consented** → one grounded LLM call returns a structured ``understanding``
  block *and* the proposed next ``action`` (so the orchestrator makes a single call). The AI
  output is then **validated + normalized deterministically** before anything is applied.
* **AI off / no key / call failed** → a robust **deterministic** parse (text normalization +
  the tested rule parser + canonicalization), labelled lower-confidence, with a gentle "less
  robust without AI" flag.

Hard invariants (this module never weakens the safety model):

* It only *extracts the experiment set-up* — material, leachant, masses, volumes, time,
  temperature, CO₂ cover, target elements, desired outputs. It **never** extracts (and
  defensively strips) a material composition, a release/dissolution fraction, a measured value,
  a computed pH/result, or a validation status — those come only from the user's confirmed
  material profile / release model / measured data, never from free text.
* It **never silently invents** a value. A value the model *assumed* (e.g. "room temp" → 25 °C)
  is returned as an assumption flagged ``needs_confirmation``; an impossible value (negative
  mass, out-of-range temperature/concentration) is **rejected** and turned into a question.
* It does **not** execute anything and is off the scientific result path (pinned by
  ``tests/test_ai_boundary.py``). Like the orchestrator it may import the AI client; it imports
  no executor and no comparison/residual/mapping code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..ai import client as ai_client
from ..ai import config as ai_config
from ..ai.import_assist import _message_text, _parse_json   # tested defensive helpers
from ..simulation import scenario_schema as S
from . import agent_actions as A
from . import agent_prompts, agent_state, domains

__all__ = [
    "ExtractionResult", "extract", "normalize_text", "classify_message",
    "canonical_leachant", "validate_value", "repair_understanding",
    "build_understanding_card", "SRC_AI", "SRC_RULE", "SRC_RULE_FALLBACK",
]

MAX_TOKENS = 1500

# Source tags for an extraction.
SRC_AI = "ai"
SRC_RULE = "rule"
SRC_RULE_FALLBACK = "rule_fallback"   # AI was attempted but failed → deterministic used

# Standing assumption reason for an assumed-ambient temperature.
ROOM_TEMP_REASON = "room temperature / ambient — assumed, no explicit value (please confirm)"

# Gentle note shown once when running without AI on free-form text.
LIMITED_WITHOUT_AI_NOTE = (
    "Heads up: AI is off, so my understanding of free-form, typo-filled text is more limited. "
    "I'll do my best with rules — enable AI in **Settings** for more robust interpretation.")

# --------------------------------------------------------------------------- #
# The set-up fields this layer is allowed to extract (everything else is ignored).
# Deliberately EXCLUDES composition / release fraction / results / validation.
# --------------------------------------------------------------------------- #
SCALAR_FIELDS = (
    "material_name", "material_type", "solid_mass_g", "liquid_volume_mL",
    "leachant_type", "leachant_concentration_M", "time_min", "temperature_C",
    "CO2_condition", "cover_condition", "filter_size_um", "centrifuge_used", "filtration_used",
)
LIST_FIELDS = ("target_elements", "desired_outputs")
EXTRACTABLE_FIELDS = SCALAR_FIELDS + LIST_FIELDS

# Fields that must be strictly positive (a non-positive value is impossible → rejected).
POSITIVE_FIELDS = ("solid_mass_g", "liquid_volume_mL", "time_min", "leachant_concentration_M",
                   "filter_size_um")

# Plausibility bounds (inclusive) — outside → rejected and turned into a question.
NUMERIC_BOUNDS = {
    "solid_mass_g": (0.0, 1.0e6),
    "liquid_volume_mL": (0.0, 1.0e7),
    "leachant_concentration_M": (0.0, 30.0),       # ~ saturation ceiling
    "time_min": (0.0, 5.0e6),
    "temperature_C": (-50.0, 1000.0),              # aqueous leaching; >100 unusual (noted)
    "filter_size_um": (0.0, 1000.0),
    "liquid_solid_ratio": (0.0, 1.0e5),
}
# Above this temperature (°C) a leaching run is physically suspect — keep it, but warn.
_HIGH_TEMP_NOTE_C = 100.0

# Human labels for the card / change notes.
FIELD_LABELS = {
    "material_name": "material", "material_type": "material type",
    "solid_mass_g": "solid mass (g)", "liquid_volume_mL": "liquid volume (mL)",
    "leachant_type": "leachant", "leachant_concentration_M": "concentration (M)",
    "time_min": "time (min)", "temperature_C": "temperature (°C)",
    "CO2_condition": "CO₂ cover", "cover_condition": "cover",
    "filter_size_um": "filter size (µm)", "target_elements": "target elements",
    "desired_outputs": "desired outputs",
}


def _label(field_name: str) -> str:
    return FIELD_LABELS.get(field_name, field_name)


# --------------------------------------------------------------------------- #
# Deterministic text normalization (typos / informal units / spacing)
# --------------------------------------------------------------------------- #
# Curated spelling/abbreviation fixes → a canonical phrase the downstream rule parser AND the
# domain classifier already understand. Conservative + ordered (specific before generic). Each
# entry is (pattern, replacement, human-note-template).  Applied case-insensitively.
_SPELLING_FIXES = [
    # materials
    (r"\bclass\s*c\s*fl[iy]\s*ash\b", "Class C fly ash", "Class C fly ash"),
    (r"\bclass\s*f\s*fl[iy]\s*ash\b", "Class F fly ash", "Class F fly ash"),
    (r"\bfl[iy]\s*ash\b", "fly ash", "fly ash"),
    (r"\bflyash\b", "fly ash", "fly ash"),
    (r"\bcfa\b", "Class C fly ash", "Class C fly ash"),
    (r"\bred\s*mud\b", "red mud", "red mud"),
    (r"\bredmud\b", "red mud", "red mud"),
    (r"\bbauxite\s+residue\b", "red mud", "red mud (bauxite residue)"),
    # leachants (typo-tolerant)
    (r"\bna\s*oh\b", "NaOH", "NaOH"),
    (r"\bsod[iu]um\s+hydrox[iy]?de\b", "NaOH", "NaOH (sodium hydroxide)"),
    (r"\bsodum\s+hydroxde\b", "NaOH", "NaOH (sodium hydroxide)"),
    (r"\bk\s*oh\b", "KOH", "KOH"),
    (r"\bpotass?ium\s+hydrox[iy]?de?\b", "KOH", "KOH (potassium hydroxide)"),
    (r"\bhcl\b", "HCl", "HCl"),
    (r"\bhydrochlor\w*\b", "HCl", "HCl (hydrochloric acid)"),
    # process / domain typos (help both extraction and domain routing)
    (r"\bleech(\w*)\b", r"leach\1", "leaching"),
    (r"\bcompres+ive\b", "compressive", "compressive"),
    (r"\bstren(?:g?h|ght|gh|th)\b", "strength", "strength"),
    (r"\bplstic\b", "plastic", "plastic"),
    (r"\bwaste\s+plstic\b", "waste plastic", "waste plastic"),
]

# Word numbers (small) before a time unit → digits, so "one hour" → "1 hour".
_WORD_NUMS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
              "seven": "7", "eight": "8", "nine": "9", "ten": "10", "half": "0.5"}
_WORD_NUM_RE = re.compile(
    r"\b(" + "|".join(_WORD_NUMS) + r")\s+(?=(?:hours?|hrs?|hr|h|minutes?|mins?|min)\b)", re.I)

# Leading-dot decimals: ".5" → "0.5".
_LEADING_DOT_RE = re.compile(r"(?<![\d.])\.(\d)")
# Molar 'm' written lowercase / unspaced after a number (not ml / min / mm / mg / mol) → " M".
_MOLAR_M_RE = re.compile(r"(?<=\d)\s*[mM](?![a-zA-Z])")

# A negative physical quantity (e.g. "-2 g") — the rule parser's regex drops the sign, so this
# catches an explicitly negative mass / volume / time the user typed (always impossible).
_NEG_QTY_RE = re.compile(
    r"-\s*\d+(?:\.\d+)?\s*(kg|mg|grams?|g|millilit\w*|ml|liters?|litres?|l|"
    r"minutes?|mins?|min|hours?|hrs?|hr|h)\b", re.I)


def _negative_fields(text: str) -> dict:
    """Map ``field -> the negative literal`` for any explicitly negative quantity in ``text``."""
    out: dict = {}
    for m in _NEG_QTY_RE.finditer(str(text or "")):
        unit = m.group(1).lower()
        if unit in ("kg", "mg", "g") or unit.startswith("gram"):
            out["solid_mass_g"] = m.group(0)
        elif unit in ("ml", "l") or "lit" in unit:
            out["liquid_volume_mL"] = m.group(0)
        else:
            out["time_min"] = m.group(0)
    return out


def normalize_text(text: str) -> tuple[str, list[str]]:
    """Return ``(normalized_text, notes)`` — fix common typos / informal units / spacing.

    Deterministic and idempotent on already-clean text (so it never changes the meaning of a
    well-formed prompt). ``notes`` are short human descriptions of what was interpreted, for the
    "I understood this as…" card. Never raises.
    """
    s = str(text or "")
    if not s.strip():
        return s, []
    notes: list[str] = []

    # 1) Spelling / abbreviation fixes.
    for pattern, repl, note in _SPELLING_FIXES:
        if re.search(pattern, s, flags=re.I):
            new = re.sub(pattern, repl, s, flags=re.I)
            if new != s:
                s = new
                if note and note not in notes:
                    notes.append(f"read “{note}”")

    # 2) Word numbers before a time unit.
    if _WORD_NUM_RE.search(s):
        s = _WORD_NUM_RE.sub(lambda m: _WORD_NUMS.get(m.group(1).lower(), m.group(1)) + " ", s)

    # 3) Leading-dot decimals (".5" → "0.5").
    if _LEADING_DOT_RE.search(s):
        s = _LEADING_DOT_RE.sub(r"0.\1", s)
        notes.append("normalized a leading-dot decimal (e.g. .5 → 0.5)")

    # 4) Molar 'm' → ' M' (so 0.5m / .5M parse as molarity, never as mL/min).
    if _MOLAR_M_RE.search(s):
        s = _MOLAR_M_RE.sub(" M", s)

    # Collapse the double spaces a substitution may introduce.
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s, notes


# --------------------------------------------------------------------------- #
# Canonicalization helpers (leachant / material / elements) — deterministic
# --------------------------------------------------------------------------- #
# Canonical leachant → recognised tokens (lowercase). "acid" alone is intentionally NOT mapped
# to any specific acid (ambiguous → asked), per the project's careful-acid rule.
_LEACHANT_CANON = {
    "NaOH": ("naoh", "sodium hydroxide"),
    "KOH": ("koh", "potassium hydroxide"),
    "HCl": ("hcl", "hydrochloric"),
    "H2SO4": ("h2so4", "sulfuric", "sulphuric"),
    "HNO3": ("hno3", "nitric"),
    "acetic acid": ("acetic",),
    "citric acid": ("citric",),
}
_WATER_TOKENS = ("water", "di water", "deionized water", "deionised water", "milliq", "milli-q",
                 "distilled water", "ultrapure water")


def canonical_leachant(text: str) -> tuple[str | None, bool]:
    """Return ``(canonical_leachant, ambiguous)`` from free text.

    ``ambiguous`` is True when the text clearly mentions an *acid / leachant* but does not name a
    specific one (e.g. "acid leach") — the caller should ask which, never guess HCl. Water maps
    to ``"water"``.
    """
    low = str(text or "").lower()
    for canon, needles in _LEACHANT_CANON.items():
        if any(n in low for n in needles):
            return canon, False
    if any(w in low for w in _WATER_TOKENS):
        return "water", False
    # A generic acid/base mention with no specific reagent → ambiguous (ask, don't guess).
    if re.search(r"\bacid(?:ic)?\b", low) or re.search(r"\balkal\w*\b", low) \
            or re.search(r"\bbase\b", low):
        return None, True
    return None, False


# Element symbols we accept case-insensitively (multi-letter only — safe as whole tokens). The
# single-letter K must be UPPERCASE to avoid matching a stray "k".
_ELEMENT_FULL = {
    "calcium": "Ca", "silicon": "Si", "silica": "Si", "silicate": "Si",
    "aluminium": "Al", "aluminum": "Al", "alumina": "Al", "iron": "Fe",
    "sodium": "Na", "potassium": "K", "scandium": "Sc",
    "rare earth": "REE", "rare-earth": "REE", "rees": "REE",
}
_ELEMENT_MULTI = ("Ca", "Si", "Al", "Fe", "Na", "Sc", "REE")
_ELEMENT_ORDER = ("Ca", "Si", "Al", "Fe", "Na", "K", "Sc", "REE")


def extract_elements(text: str) -> list[str]:
    """Permissive element extraction (case-insensitive multi-letter symbols + full names).

    Tolerant of lowercase mentions like "ph ca si" / "fe al na sc"; conservative for the
    single-letter K (uppercase only) so a stray "k" is never read as potassium. Returns symbols
    in the project's canonical order.
    """
    s = str(text or "")
    low = s.lower()
    found: set[str] = set()
    for name, sym in _ELEMENT_FULL.items():
        if name in low:
            found.add(sym)
    for sym in _ELEMENT_MULTI:
        if re.search(r"\b" + sym + r"\b", s, flags=re.I):
            found.add(sym)
    if re.search(r"\bK\b", s):                 # potassium — uppercase only
        found.add("K")
    return [s for s in _ELEMENT_ORDER if s in found]


# --------------------------------------------------------------------------- #
# Multi-step (thermal pretreatment → leach) detection (robustness fix A)
# --------------------------------------------------------------------------- #
_THERMAL_CUE_RE = re.compile(
    r"\b(calcin\w*|thermal|heat\w*|roast\w*|sinter\w*|pyroly\w*|fir(?:e|ed|ing)|furnace|"
    r"kiln|anneal\w*)\b", re.I)
_LEACH_CUE_RE = re.compile(r"\b(leach\w*|dissolv\w*|leachate)\b", re.I)
_TEMP_FIND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°\s*|deg(?:rees?)?\s*)?[cC]\b")
# A temperature at/above this is a thermal-treatment temperature, not an aqueous-leach temperature.
_PRETREAT_TEMP_MIN_C = 150.0


def pretreatment_temperature(text: str):
    """The highest temperature ≥150 °C in the text (a calcination/thermal-treatment temp), else None."""
    hot = [float(m) for m in _TEMP_FIND_RE.findall(str(text or "")) if float(m) >= _PRETREAT_TEMP_MIN_C]
    return max(hot) if hot else None


def is_thermal_pretreatment_then_leach(text: str) -> bool:
    """True when the text describes a thermal pretreatment (≥150 °C) *and* a subsequent leach.

    For such a workflow the high temperature is the calcination temperature; the aqueous-leach
    temperature is a *separate* (usually unstated) quantity, so the high temp must not be stored
    as the leach ``temperature_C``.
    """
    s = str(text or "")
    return bool(_THERMAL_CUE_RE.search(s) and _LEACH_CUE_RE.search(s)
                and pretreatment_temperature(s) is not None)


# --------------------------------------------------------------------------- #
# Out-of-scope elements (recognised by name/symbol but NOT in the engine's set) — fix C
# --------------------------------------------------------------------------- #
# Common metals a user may ask about that the current PHREEQC leaching element set
# (Ca/Si/Al/Fe/Na/K/Sc/REE) does not handle. Captured + surfaced, never silently dropped.
_UNSUPPORTED_BY_NAME = {
    "nickel": "Ni", "cobalt": "Co", "manganese": "Mn", "lithium": "Li", "copper": "Cu",
    "zinc": "Zn", "lead": "Pb", "chromium": "Cr", "cadmium": "Cd", "molybdenum": "Mo",
    "tungsten": "W", "magnesium": "Mg", "titanium": "Ti", "vanadium": "V",
}
_UNSUPPORTED_SYMBOLS = ("Ni", "Co", "Mn", "Li", "Cu", "Zn", "Pb", "Cr", "Cd", "Mo", "Mg", "Ti", "V")
# Lowercase element-like tokens used to detect a "cluster" (so 'ni co mn' is read as elements but
# a stray 'co' word is not).
_ELEMENT_CLUSTER_TOKENS = {s.lower() for s in _UNSUPPORTED_SYMBOLS + _ELEMENT_MULTI + ("K",)}


def detect_unsupported_elements(text: str) -> list:
    """Element symbols the user requested that are outside the engine's element set.

    Full names ("nickel") are always matched; an UPPERCASE symbol ("Ni") anywhere; a lowercase
    symbol ("ni") only inside a cluster of ≥2 adjacent element-like tokens (so "ni co mn" is
    caught, an incidental "co" word is not).
    """
    s = str(text or "")
    low = s.lower()
    found: list = []
    for name, sym in _UNSUPPORTED_BY_NAME.items():
        if re.search(r"\b" + name + r"\b", low) and sym not in found:
            found.append(sym)
    for sym in _UNSUPPORTED_SYMBOLS:
        if re.search(r"\b" + sym + r"\b", s) and sym not in found:        # uppercase, case-sensitive
            found.append(sym)
    tokens = re.findall(r"[A-Za-z]+", s)
    for i, tok in enumerate(tokens):
        sym = tok.capitalize()
        if sym in _UNSUPPORTED_SYMBOLS and sym not in found:
            prev_el = i > 0 and tokens[i - 1].lower() in _ELEMENT_CLUSTER_TOKENS
            next_el = i < len(tokens) - 1 and tokens[i + 1].lower() in _ELEMENT_CLUSTER_TOKENS
            if prev_el or next_el:
                found.append(sym)
    return found


# --------------------------------------------------------------------------- #
# Numeric validation / schema repair
# --------------------------------------------------------------------------- #
@dataclass
class _Reject:
    field: str
    value: object
    reason: str


def validate_value(field_name: str, value) -> tuple[object, str | None, str | None]:
    """Validate one extracted field → ``(clean_value, reject_reason, note)``.

    * ``reject_reason`` set (and ``clean_value`` None) → the value is impossible and must NOT be
      applied (turned into a clarifying question instead). e.g. negative mass, out-of-range
      concentration/temperature.
    * ``note`` set → the value is kept but worth flagging (e.g. an unusually high temperature).
    """
    if field_name in NUMERIC_BOUNDS or field_name in POSITIVE_FIELDS:
        num = S.as_float(value)
        if num is None:
            return None, None, None        # not a number → silently ignored (not "impossible")
        if field_name in POSITIVE_FIELDS and num <= 0:
            return None, f"{_label(field_name)} must be greater than zero (got {num:g})", None
        lo, hi = NUMERIC_BOUNDS.get(field_name, (float("-inf"), float("inf")))
        if num < lo or num > hi:
            return None, (f"{_label(field_name)} = {num:g} is outside the plausible range "
                          f"[{lo:g}, {hi:g}]"), None
        note = None
        if field_name == "temperature_C" and num > _HIGH_TEMP_NOTE_C:
            note = (f"{num:g} °C is high for aqueous leaching — kept, but double-check it isn't a "
                    "thermal-treatment step.")
        return num, None, note
    return value, None, None


# --------------------------------------------------------------------------- #
# The extraction result
# --------------------------------------------------------------------------- #
@dataclass
class ExtractionResult:
    """What :func:`extract` returns for one user message."""

    delta: dict = field(default_factory=dict)            # explicit fields → values to merge
    changes: list = field(default_factory=list)          # [{field,label,old,new}] vs current
    assumption_specs: list = field(default_factory=list)  # [(field, value, reason)]
    drop_assumption_fields: tuple = ()                    # assumptions made obsolete this turn
    ambiguous_fields: list = field(default_factory=list)  # field names to ask about
    rejected: list = field(default_factory=list)          # [{field,value,reason}] impossible
    normalizations: list = field(default_factory=list)    # human notes ("read NaOH", …)
    confidence: float = 0.0
    source: str = SRC_RULE                               # ai / rule / rule_fallback
    used_ai: bool = False
    limited_without_ai: bool = False
    ai_error: str | None = None
    domain_hint: str | None = None
    action: object = None                                # an AgentAction (AI path) or None
    assistant_message: str = ""                          # the model's conversational lead (AI)
    # A calcination / thermal-pretreatment temperature is NOT the aqueous-leach temperature; it
    # is captured here so it never lands in the scenario's leach temperature_C.
    pretreatment_temperature_C: float | None = None
    # Elements the user asked about that are outside the engine's set (Ca/Si/Al/Fe/Na/K/Sc/REE);
    # captured + surfaced, never silently dropped.
    unsupported_elements: list = field(default_factory=list)

    @property
    def has_understanding(self) -> bool:
        return bool(self.delta or self.ambiguous_fields or self.rejected)


# --------------------------------------------------------------------------- #
# Change / conflict detection
# --------------------------------------------------------------------------- #
def compute_changes(delta: dict, current_flat: dict) -> list:
    """List the fields the new delta *changes* (a correction), with old → new values.

    A field already set to a different value (or list) is a change/conflict the user should see.
    A field that was empty is a first-time fill (not reported as a change).
    """
    changes: list = []
    for key, new in delta.items():
        old = current_flat.get(key)
        if key in LIST_FIELDS:
            old_list = list(old or [])
            # A list field only "changes" when previously-set items are dropped/replaced; pure
            # additions are not a conflict.
            if old_list and not set(old_list).issubset(set(new or [])):
                changes.append({"field": key, "label": _label(key), "old": old_list, "new": new})
        elif old is not None and old != new:
            changes.append({"field": key, "label": _label(key), "old": old, "new": new})
    return changes


# --------------------------------------------------------------------------- #
# Deterministic delta (normalize → tested rule parser → canonicalize → validate)
# --------------------------------------------------------------------------- #
def _deterministic_delta(message: str, current_flat: dict) -> ExtractionResult:
    """Robust rule-based extraction (no AI). Reuses the tested presence-gated extractor on the
    *normalized* text, then layers permissive element extraction + validation on top."""
    normalized, notes = normalize_text(message)
    raw_delta, temp_assumed = agent_state.extract_explicit_delta(normalized)

    # Permissive elements (case-insensitive) — union with whatever the rule parser found.
    els = extract_elements(normalized)
    if els:
        merged = list(dict.fromkeys(list(raw_delta.get("target_elements") or []) + els))
        raw_delta["target_elements"] = merged

    # Ambiguity: a generic acid/base with no named reagent.
    ambiguous: list[str] = []
    if not raw_delta.get("leachant_type"):
        _lc, amb = canonical_leachant(normalized)
        if amb:
            ambiguous.append("leachant_type")

    delta, rejected, val_notes = _validate_delta(raw_delta)

    # Explicitly-negative quantities (the rule parser drops the sign) → reject + drop.
    for field_name, literal in _negative_fields(normalized).items():
        if field_name in delta:
            del delta[field_name]
        rejected.append({"field": field_name, "value": literal,
                         "reason": f"{_label(field_name)} cannot be negative ({literal.strip()})"})

    assumption_specs, drop_fields = _temp_assumptions(delta, temp_assumed)
    confidence = _rule_confidence(delta)
    changes = compute_changes(delta, current_flat)
    return ExtractionResult(
        delta=delta, changes=changes, assumption_specs=assumption_specs,
        drop_assumption_fields=drop_fields, ambiguous_fields=ambiguous, rejected=rejected,
        normalizations=notes + val_notes, confidence=confidence, source=SRC_RULE, used_ai=False)


def _validate_delta(raw_delta: dict) -> tuple[dict, list, list]:
    """Validate every field of a delta → ``(clean_delta, rejected, notes)``.

    Fields outside the set-up set are dropped; impossible numerics are rejected (not applied);
    empty list fields are omitted so they are never treated as a (no-op) change.
    """
    clean: dict = {}
    rejected: list = []
    notes: list = []
    for key, value in raw_delta.items():
        if key not in EXTRACTABLE_FIELDS:
            continue                                   # never apply anything off the set-up set
        if key in LIST_FIELDS:
            items = list(value or [])
            if items:
                clean[key] = items
            continue
        val, reject_reason, note = validate_value(key, value)
        if reject_reason:
            rejected.append({"field": key, "value": value, "reason": reject_reason})
            continue
        if val is None and (key in NUMERIC_BOUNDS or key in POSITIVE_FIELDS):
            continue                                   # numeric field, unparseable value → ignore
        clean[key] = val if val is not None else value
        if note:
            notes.append(note)
    return clean, rejected, notes


def _temp_assumptions(delta: dict, temp_assumed: bool) -> tuple[list, tuple]:
    """Build the temperature assumption spec (when assumed) or mark it obsolete (when explicit)."""
    if "temperature_C" in delta and temp_assumed:
        return [("temperature_C", delta["temperature_C"], ROOM_TEMP_REASON)], ()
    if "temperature_C" in delta and not temp_assumed:
        return [], ("temperature_C",)              # an explicit value clears any prior assumption
    return [], ()


def _rule_confidence(delta: dict) -> float:
    """A modest confidence for the deterministic path (well below a clean AI extraction)."""
    core = ("solid_mass_g", "liquid_volume_mL", "leachant_type", "time_min",
            "leachant_concentration_M", "material_name")
    found = sum(1 for k in core if delta.get(k) is not None)
    return round(min(0.55, 0.55 * found / len(core)), 2)


# --------------------------------------------------------------------------- #
# AI understanding → validated delta
# --------------------------------------------------------------------------- #
def _clamp_co2(value):
    v = S.as_str(value)
    return v if v in S.CO2_CONDITION_ALLOWED else None


def _clamp_cover(value):
    v = S.as_str(value)
    return v if v in S.COVER_CONDITIONS else None


def _clean_elements(value):
    out: list[str] = []
    for el in S.as_str_list(value):
        norm = ("REE" if el.upper() == "REE"
                else el[:1].upper() + el[1:].lower() if len(el) > 1 else el.upper())
        token = norm if norm in S.RECOGNIZED_ELEMENTS else el
        if token not in out:
            out.append(token)
    return out


def repair_understanding(understanding: dict, *, current_flat: dict) -> ExtractionResult:
    """Turn a model ``understanding`` block into a validated :class:`ExtractionResult`.

    Defensive by construction: coerces types, clamps CO₂/cover/elements, **drops any field
    outside the set-up set** (so a sneaked-in composition / release fraction / result is
    ignored), validates numerics (rejecting impossible ones), and records the model's flagged
    assumptions (each ``needs_confirmation``) + ambiguous fields. Never raises.
    """
    u = understanding if isinstance(understanding, dict) else {}

    raw: dict = {
        "material_name": S.as_str(u.get("material_name")),
        "material_type": S.as_str(u.get("material_type")),
        "solid_mass_g": S.as_float(u.get("solid_mass_g")),
        "liquid_volume_mL": S.as_float(u.get("liquid_volume_mL")),
        "leachant_type": S.as_str(u.get("leachant_type")),
        "leachant_concentration_M": S.as_float(u.get("leachant_concentration_M")),
        "time_min": S.as_float(u.get("time_min")),
        "temperature_C": S.as_float(u.get("temperature_C")),
        "CO2_condition": _clamp_co2(u.get("CO2_condition")),
        "cover_condition": _clamp_cover(u.get("cover_condition")),
        "filter_size_um": S.as_float(u.get("filter_size_um")),
        "centrifuge_used": S.as_bool(u.get("centrifuge_used")),
        "filtration_used": S.as_bool(u.get("filtration_used")),
        "target_elements": _clean_elements(u.get("target_elements")),
        "desired_outputs": S.as_str_list(u.get("desired_outputs")),
    }
    # Canonicalize a free-text leachant name the model may have echoed verbatim.
    if raw.get("leachant_type"):
        canon, _amb = canonical_leachant(raw["leachant_type"])
        if canon:
            raw["leachant_type"] = canon
    # Keep only stated (non-None / non-empty) fields.
    raw = {k: v for k, v in raw.items()
           if v is not None and not (k in LIST_FIELDS and not v)}

    delta, rejected, notes = _validate_delta(raw)

    # Assumptions the model flagged (each needs confirmation; never silently trusted). An
    # assumed value for a set-up field the model left null in `understanding` is tentatively
    # folded into the delta (so the workflow can proceed) AND kept as an assumption to confirm —
    # exactly mirroring the deterministic "room temp → 25 °C, flagged" behaviour.
    assumption_specs: list = []
    for a in (u.get("assumptions") or []):
        if isinstance(a, dict) and S.as_str(a.get("field")):
            f = S.as_str(a.get("field"))
            reason = S.as_str(a.get("reason")) or "assumed by the assistant — please confirm"
            assumption_specs.append((f, a.get("assumed_value"), reason))
            if f in EXTRACTABLE_FIELDS and f not in delta:
                val, reject_reason, _note = validate_value(f, a.get("assumed_value"))
                if reject_reason is None:
                    folded = val if val is not None else a.get("assumed_value")
                    if folded is not None and not (
                            f in (NUMERIC_BOUNDS.keys() | set(POSITIVE_FIELDS)) and val is None):
                        delta[f] = folded
    # An explicit (non-assumed) temperature clears a prior temperature assumption.
    assumed_fields = {spec[0] for spec in assumption_specs}
    drop_fields: tuple = ("temperature_C",) if (
        "temperature_C" in delta and "temperature_C" not in assumed_fields) else ()

    # The model's ambiguous_fields is authoritative on the AI path (it saw the full message).
    ambiguous = [str(x) for x in (u.get("ambiguous_fields") or []) if str(x).strip()]

    confidence = max(0.0, min(1.0, S.as_float(u.get("confidence")) or 0.0)) or _rule_confidence(delta)
    domain_hint = domains._hint_domain(u.get("domain_hint"))
    changes = compute_changes(delta, current_flat)
    # Out-of-scope elements + a calcination temperature the model flagged (fixes A + C). These are
    # NOT scenario fields, so they ride on the result, not the delta (extract() also backstops them).
    pretreat = S.as_float(u.get("pretreatment_temperature_C"))
    target = delta.get("target_elements") or []
    unsupported = [str(e).strip().capitalize() for e in S.as_str_list(u.get("unsupported_elements"))
                   if str(e).strip() and str(e).strip().capitalize() not in target]
    return ExtractionResult(
        delta=delta, changes=changes, assumption_specs=assumption_specs,
        drop_assumption_fields=drop_fields, ambiguous_fields=ambiguous, rejected=rejected,
        normalizations=notes, confidence=confidence, source=SRC_AI, used_ai=True,
        domain_hint=domain_hint, pretreatment_temperature_C=pretreat,
        unsupported_elements=unsupported)


# --------------------------------------------------------------------------- #
# The AI call (one grounded turn → understanding + action)
# --------------------------------------------------------------------------- #
def _extract_with_ai(message, state, *, client, model, current_flat) -> ExtractionResult | None:
    """Run the single grounded LLM turn. Returns ``None`` (never raises) when AI is unavailable
    or the response is unusable — the caller then uses the deterministic path."""
    resolved = ai_client.get_client(client, model=model)
    if not resolved.ok or resolved.client is None:
        return None
    try:
        resp = resolved.client.messages.create(
            model=ai_config.resolve_model(model), max_tokens=MAX_TOKENS,
            system=agent_prompts.SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": agent_prompts.build_user_prompt(state, message)}])
    except Exception:                                       # noqa: BLE001 — never crash the chat
        return None
    payload = _parse_json(_message_text(resp))
    if not isinstance(payload, dict):
        return None

    action = A.parse_action(payload)                        # strips forbidden keys
    understanding = payload.get("understanding")
    if isinstance(understanding, dict):
        result = repair_understanding(understanding, current_flat=current_flat)
        top_conf = S.as_float(payload.get("confidence"))    # confidence is a top-level field
        if top_conf is not None:
            result.confidence = max(0.0, min(1.0, top_conf))
    else:
        # The model proposed an action but no structured understanding → parse fields with rules,
        # but still credit the AI for choosing the action.
        result = _deterministic_delta(message, current_flat)
        result.source = SRC_AI
    result.used_ai = True
    result.action = action
    result.assistant_message = str(payload.get("assistant_message") or "").strip()
    return result


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def extract(message: str, *, state, client=None, model=None, use_ai: bool = True) -> ExtractionResult:
    """Understand one user message → an :class:`ExtractionResult` (never raises).

    Uses the LLM when available + ``use_ai`` (one grounded call returning the structured
    understanding *and* the proposed action), validated/normalized deterministically; otherwise
    a robust rule-based parse, flagged as more limited. The orchestrator applies the returned
    delta through the *pure* :meth:`agent_state.AgentState.apply_delta`.
    """
    current_flat = state.scenario.to_flat_dict()
    ai_on = (client is not None) or ai_config.is_enabled()

    result = None
    if use_ai and ai_on:
        result = _extract_with_ai(message, state, client=client, model=model,
                                  current_flat=current_flat)
        if result is None:
            # AI was on but the call/parse failed → deterministic fallback, clearly degraded.
            result = _deterministic_delta(message, current_flat)
            result.source = SRC_RULE_FALLBACK
            result.limited_without_ai = True
            result.ai_error = "AI extraction was unavailable for this message — used the rule parser."
    if result is None:
        result = _deterministic_delta(message, current_flat)
        result.limited_without_ai = not ai_on

    return _separate_multistep_and_capture_unsupported(message, result)


def _separate_multistep_and_capture_unsupported(message: str, result: ExtractionResult):
    """Post-process both paths: keep a calcination temperature out of the leach ``temperature_C``
    (fix A), and capture out-of-scope elements (fix C). Defensive for the AI path too."""
    # A: a thermal-pretreatment temperature is NOT the aqueous-leach temperature.
    if is_thermal_pretreatment_then_leach(message):
        result.pretreatment_temperature_C = (result.pretreatment_temperature_C
                                             or pretreatment_temperature(message))
        leach_t = result.delta.get("temperature_C")
        if leach_t is not None and leach_t >= _PRETREAT_TEMP_MIN_C:
            result.delta.pop("temperature_C", None)
            result.assumption_specs = [a for a in result.assumption_specs
                                       if a[0] != "temperature_C"]
            result.changes = [c for c in result.changes if c.get("field") != "temperature_C"]
            if "temperature_C" not in result.ambiguous_fields:
                result.ambiguous_fields.append("temperature_C")   # ask for the LEACH temperature
    # C: out-of-scope elements (e.g. Ni / Co / Mn) — capture, never silently drop.
    unsupported = detect_unsupported_elements(message)
    if unsupported:
        result.unsupported_elements = list(dict.fromkeys(
            list(result.unsupported_elements) + unsupported))
    return result


# --------------------------------------------------------------------------- #
# Convenience: typo-tolerant domain classification (normalize → classify)
# --------------------------------------------------------------------------- #
def classify_message(text: str, *, hint: str | None = None) -> str:
    """Classify a (possibly messy) message into a domain, normalizing typos first."""
    normalized, _notes = normalize_text(text)
    return domains.classify(normalized, hint=hint)


# --------------------------------------------------------------------------- #
# The "I understood this as…" card (plain dict — the UI renders it; state stays pure)
# --------------------------------------------------------------------------- #
def build_understanding_card(state, extraction: ExtractionResult) -> dict:
    """Build the compact card the UI shows after each turn (the merged picture + this turn's
    interpretation). Pure data — no Streamlit, no AI types leak into ``state``."""
    flat = state.scenario.to_flat_dict()
    leachant = flat.get("leachant_type") or "—"
    conc = flat.get("leachant_concentration_M")
    if conc is not None:
        leachant = f"{leachant} @ {conc:g} M"
    key_vars = []
    for f in ("solid_mass_g", "liquid_volume_mL", "liquid_solid_ratio", "time_min",
              "temperature_C", "CO2_condition"):
        v = flat.get(f)
        if v is not None:
            key_vars.append(f"{_label(f)}: {v}")

    assumptions = [{"field": f, "value": v, "reason": r} for (f, v, r) in extraction.assumption_specs]
    # Carry any standing scenario assumptions that still need confirmation too.
    for a in state.assumptions:
        if a.field not in {x["field"] for x in assumptions}:
            assumptions.append({"field": a.field, "value": a.assumed_value, "reason": a.reason})

    return {
        "domain": state.domain,
        "domain_label": domains.label(state.domain),
        "executable": domains.is_executable(state.domain),
        "material": flat.get("material_name") or "—",
        "leachant": leachant,
        "key_variables": key_vars,
        "target_elements": list(flat.get("target_elements") or []),
        "desired_outputs": list(flat.get("desired_outputs") or []),
        "missing": [m.label for m in state.missing_fields][:3],
        "assumptions": assumptions,
        "ambiguous": list(extraction.ambiguous_fields),
        "rejected": list(extraction.rejected),
        "pretreatment_temperature_C": extraction.pretreatment_temperature_C,
        "unsupported_elements": list(extraction.unsupported_elements),
        "changes": list(extraction.changes),
        "normalizations": list(extraction.normalizations),
        "confidence": round(float(extraction.confidence or 0.0), 2),
        "source": extraction.source,
        "source_label": {SRC_AI: "AI understanding", SRC_RULE: "rule-based reading",
                         SRC_RULE_FALLBACK: "rule-based fallback (AI unavailable)"}.get(
            extraction.source, extraction.source),
        "used_ai": extraction.used_ai,
    }
