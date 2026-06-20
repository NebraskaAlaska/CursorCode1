"""Deterministic chat → material-composition / release-model / database extraction.

This module fills a deliberate gap in the agent's understanding layer **safely**. The LLM is
*forbidden* from supplying a composition, a release fraction, or a database
(:data:`flyash_phreeqc_ml.agent.agent_actions.FORBIDDEN_ARGUMENT_KEYS`) — those must never be
*invented* by a language model. But a user who literally types

    "synthetic demo composition SiO2 34 wt%, Al2O3 18, CaO 24, Fe2O3 7 … use global 1% release,
     phreeqc.dat"

should not then be forced to re-type it all into a strict-format text box. So this module parses
**only what the user literally wrote** (regex over oxide tokens + release / database phrases) into
an **unconfirmed DRAFT** the user must still explicitly confirm before it can feed a PHREEQC input
preview. Transcribing the user's own words is not invention — and the draft is never usable until
confirmed, so the confirmation gate is fully preserved.

Hard properties (mirroring the project's safety rules):

* **No AI** — pure regex (it imports no AI client). **No executor / result path** — it imports only
  the materials composition schema + the source-term constructors (both pure planning helpers).
* **Never invents** a missing oxide, a release fraction, a measured value, or a validation status.
  It extracts the user's literal numbers and stops; absent information stays absent.
* **Never auto-confirms** a composition and **never auto-runs** anything. A parsed composition lands
  as :data:`flyash_phreeqc_ml.materials.profile_schema.STATUS_DRAFT`; only an explicit confirmation
  (UI checkbox / chat "I confirm this composition") — and only when it *validates* — flips it usable.
* **Stable identity** — a re-statement of the same composition keeps a confirmed profile confirmed,
  and a material-name change never rebuilds (so confirmation is never silently lost).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..materials import profile_schema as mp
from ..materials import profile_validation
from ..simulation import source_terms

# --------------------------------------------------------------------------- #
# Stable identity for the assistant's chat-parsed composition profile
# --------------------------------------------------------------------------- #
# One stable id per conversation so a display-name change never rebuilds the profile and the
# preview signature (which keys on profile_id) stays consistent across turns.
ASSISTANT_PROFILE_ID = "assistant-composition"
DEFAULT_MATERIAL_NAME = "Provided material"

# --------------------------------------------------------------------------- #
# Oxide vocabulary (canonical oxide → accepted literal tokens, case-insensitive)
# --------------------------------------------------------------------------- #
# Synonyms cover the formula, the common mineral/oxide name, and the frequent "0-for-O" typo on a
# *multi-atom* oxide (al203 → Al2O3) where the zero cannot be a value. A bare metal+zero (Ca0) is
# deliberately NOT mapped — it is ambiguous (CaO vs Ca = 0) and is flagged, never silently guessed.
_OXIDE_SYNONYMS: dict[str, list[str]] = {
    "SiO2": ["sio2", "si02", "silica", "silicon dioxide", "silicon oxide"],
    "Al2O3": ["al2o3", "al203", "alumina", "aluminium oxide", "aluminum oxide"],
    "CaO": ["cao", "calcium oxide", "lime"],
    "Fe2O3": ["fe2o3", "fe203", "ferric oxide", "iron oxide", "iron(iii) oxide", "iron iii oxide"],
    "FeO": ["feo", "ferrous oxide", "iron(ii) oxide"],
    "MgO": ["mgo", "magnesia", "magnesium oxide"],
    "Na2O": ["na2o", "na20", "soda", "sodium oxide"],
    "K2O": ["k2o", "k20", "potash", "potassium oxide"],
    "TiO2": ["tio2", "titania", "titanium dioxide", "titanium oxide"],
    "SO3": ["so3", "sulfur trioxide", "sulphur trioxide", "sulfur oxide"],
    "P2O5": ["p2o5", "p205", "phosphorus pentoxide", "phosphorus oxide"],
    "MnO": ["mno", "manganese oxide", "manganous oxide"],
    "Mn2O3": ["mn2o3", "mn203"],
    "V2O5": ["v2o5", "v205", "vanadium pentoxide"],
    "Cr2O3": ["cr2o3", "cr203", "chromium oxide", "chromia"],
    "SrO": ["sro", "strontium oxide"],
    "BaO": ["bao", "barium oxide"],
}
# LOI / "other" / balance — kept in the oxide sum, never treated as an element.
_LOI_CANON = "LOI/Other"
_LOI_TOKENS = ["loss on ignition", "loi/other", "loi / other", "loi other", "l.o.i.", "l.o.i",
               "loi", "other", "balance"]

# token (lowercase) -> (kind, canonical). kind is "oxide" or "loi".
_TOKEN_CANON: dict[str, tuple[str, str]] = {}
for _canon, _toks in _OXIDE_SYNONYMS.items():
    for _t in _toks:
        _TOKEN_CANON[_t] = ("oxide", _canon)
for _t in _LOI_TOKENS:
    _TOKEN_CANON[_t] = ("loi", _LOI_CANON)

# Longest token first so "calcium oxide" / "loi other" win over "lime" / "loi" / "other".
_ALL_TOKENS = sorted(_TOKEN_CANON, key=len, reverse=True)
_TOKEN_ALT = "|".join(re.escape(t) for t in _ALL_TOKENS)
# <token> [optional : or =] [optional spaces] <number> [optional unit]. A leading boundary stops a
# token matching inside a larger word; a trailing digit requirement stops an oxide *name* with no
# value (e.g. "calcium oxide present") from matching.
_PAIR_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<species>" + _TOKEN_ALT + r")"
    r"\s*[:=]?\s*"
    r"(?P<value>[-+]?\d+(?:\.\d+)?)"
    r"\s*(?P<unit>wt\s*%|wt\b|%)?",
    re.IGNORECASE)

# Ambiguous metal+literal-zero tokens (CaO mistyped "Ca0" vs "Ca = 0") — flagged, never parsed.
_AMBIGUOUS_ZERO_RE = re.compile(r"(?<![A-Za-z0-9])(ca0|mg0|fe0|mn0|sr0|ba0)(?![A-Za-z0-9])", re.I)

# Composition is only inferred when the user clearly gave one: ≥2 oxide/value pairs, OR ≥1 pair with
# an explicit composition cue word (so a stray "SiO2 2" in prose is not treated as a full assay).
_COMPOSITION_CUE_RE = re.compile(
    r"\b(composition|oxide[s]?|assay|xrf|wt\s*%|weight\s*percent|bulk\s+chemistry)\b", re.I)


@dataclass
class CompositionParse:
    """A composition transcribed from the user's words (oxide basis, draft until confirmed)."""

    entries: list = field(default_factory=list)            # list[mp.CompositionEntry]
    basis: str = mp.BASIS_OXIDE_WT
    total_pct: float | None = None
    assumed_units: bool = False                            # no explicit %/wt → wt% assumed
    warnings: list = field(default_factory=list)
    blocking: list = field(default_factory=list)           # impossible values → block confirmation
    material_name: str | None = None

    def canonical_text(self) -> str:
        """The ``species value`` text used to seed / display the composition box."""
        return "\n".join(f"{e.species} {_fmt_num(e.value)}" for e in self.entries)


@dataclass
class ReleaseParse:
    """A release model transcribed from the user's words (always a *user assumption*)."""

    model: object = None                                   # source_terms.DissolutionModel
    is_global: bool = False
    global_pct: float | None = None                        # for the UI widget (e.g. 1.0)
    per_element_pct: dict = field(default_factory=dict)    # {El: percent} for display
    description: str = ""
    warnings: list = field(default_factory=list)


@dataclass
class DatabaseParse:
    """A database the user named (a *selection*, not a fabricated path)."""

    name: str = ""
    warnings: list = field(default_factory=list)


@dataclass
class ChatSetup:
    """Everything the deterministic parser found in one message (any field may be None/empty)."""

    composition: CompositionParse | None = None
    release: ReleaseParse | None = None
    database: DatabaseParse | None = None
    confirm_requested: bool = False


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #
def _fmt_num(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _normalize_spaces(text: str) -> str:
    """Collapse runs of spaces/tabs to one (keeps newlines) so multi-word tokens match."""
    return re.sub(r"[ \t]+", " ", str(text or ""))


def parse_oxide_composition(text: str) -> CompositionParse | None:
    """Extract an oxide composition the user literally typed, or ``None`` if there isn't one.

    Recognizes oxide formulas, common oxide/mineral names, and the unambiguous ``0``-for-``O`` typo
    on multi-atom oxides. Returns at most one entry per canonical oxide (a duplicate is kept-first +
    warned). Negative / >100 wt% single values are captured **and** flagged as blocking so the
    confirmation gate refuses them. Never raises, never invents a missing oxide.
    """
    norm = _normalize_spaces(text)
    seen: dict[str, mp.CompositionEntry] = {}
    warnings: list[str] = []
    blocking: list[str] = []
    any_explicit_unit = False

    for m in _PAIR_RE.finditer(norm):
        kind, canon = _TOKEN_CANON[m.group("species").lower()]
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        if m.group("unit"):
            any_explicit_unit = True
        species = "LOI/Other" if kind == "loi" else canon
        if value < 0:
            blocking.append(f"{species} = {_fmt_num(value)} is negative — not a valid composition.")
        elif value > 100:
            blocking.append(f"{species} = {_fmt_num(value)} wt% exceeds 100 % — check the value.")
        if species in seen:
            warnings.append(f"{species} appears more than once — kept the first value "
                            f"({_fmt_num(seen[species].value)}); please clarify if that's wrong.")
            continue
        seen[species] = mp.CompositionEntry(species=species, value=value)

    pair_count = len(seen)
    if pair_count == 0:
        return None
    if pair_count < 2 and not _COMPOSITION_CUE_RE.search(norm):
        # A single oxide+value with no "composition/oxide/wt%" cue is too weak to treat as an assay.
        return None

    # Ambiguous metal+zero tokens (e.g. "Ca0") that we deliberately did not parse — warn explicitly.
    for amb in {a.lower() for a in _AMBIGUOUS_ZERO_RE.findall(norm)}:
        metal = amb[:-1].capitalize()
        warnings.append(f"'{amb}' is ambiguous ({metal}O oxide vs {metal} = 0). I didn't assume it "
                        f"— please write the oxide (e.g. {metal}O) if you meant the oxide.")

    entries = list(seen.values())
    total = round(sum(e.value for e in entries if e.value and e.value >= 0), 4)
    assumed = not any_explicit_unit
    if assumed:
        warnings.append("No units were given — interpreting the numbers as oxide wt % (please "
                        "confirm).")
    if total < 90 or total > 102:
        warnings.append(f"Oxide total is ≈{total:g} wt% — outside the usual 90–102 % range "
                        "(components may be missing, or a value double-counted).")
    return CompositionParse(entries=entries, basis=mp.BASIS_OXIDE_WT, total_pct=total,
                            assumed_units=assumed, warnings=warnings, blocking=blocking)


# --------------------------------------------------------------------------- #
# Release model
# --------------------------------------------------------------------------- #
_RELEASE_CUE_RE = re.compile(r"\b(release|leach(?:ed|ing)?\s+fraction|dissolution\s+fraction|"
                             r"fraction\s+(?:of\s+)?(?:the\s+)?solid)\b", re.I)
# "<n>% release" / "global 1 percent release" / "1% of the solid dissolves".
_GLOBAL_RELEASE_RE = re.compile(
    r"(?:global\s+|overall\s+|a\s+|use\s+)?"
    r"([-+]?\d+(?:\.\d+)?)\s*(%|percent|per\s*cent)?\s*"
    r"(?:global\s+)?(?:release|dissolution|dissolv\w*|leach\w*)", re.I)
# "release fraction of 0.01" / "release of 1%".
_GLOBAL_RELEASE_RE2 = re.compile(
    r"(?:release|dissolution)\s+(?:fraction\s+)?(?:of\s+)?"
    r"([-+]?\d+(?:\.\d+)?)\s*(%|percent|per\s*cent)?", re.I)
# Per-element "Ca 2%", "Si 1%", "Al 0.5%".
_PER_ELEMENT_RE = re.compile(
    r"(?<![A-Za-z])(Ca|Si|Al|Fe|Na|K|Mg|S|Ti|Mn|P|Sr|Ba|V|Cr)\s*[:=]?\s*"
    r"([-+]?\d+(?:\.\d+)?)\s*(%|percent|per\s*cent)", re.I)
_ELEMENT_CANON = {e.lower(): e for e in
                  ("Ca", "Si", "Al", "Fe", "Na", "K", "Mg", "S", "Ti", "Mn", "P", "Sr", "Ba",
                   "V", "Cr")}


def _fraction_from(value: float, has_percent: bool) -> tuple[float, list[str]]:
    """Turn a parsed (value, has_percent) into a 0–1 fraction with any interpretation warning."""
    warns: list[str] = []
    if has_percent:
        frac = value / 100.0
    elif 0 < value < 1:
        frac = value                              # a bare decimal < 1 is already a fraction
    else:
        frac = value / 100.0                      # a bare integer ≥ 1 is read as a percent
        warns.append(f"Interpreted '{_fmt_num(value)}' as {_fmt_num(value)} % release "
                     "(say e.g. '0.01' for a fraction).")
    if frac > 1:
        warns.append(f"Release fraction ≈{frac:g} exceeds 100 % — physically impossible; please "
                     "check it before confirming.")
    return frac, warns


def parse_release(text: str) -> ReleaseParse | None:
    """Extract a release model the user described, or ``None``. Always a *user assumption*."""
    if not _RELEASE_CUE_RE.search(text or ""):
        return None
    low = _normalize_spaces(text)

    # Per-element fractions first (they are more specific than a single global number).
    per = {}
    per_pct = {}
    for m in _PER_ELEMENT_RE.finditer(low):
        el = _ELEMENT_CANON[m.group(1).lower()]
        frac, _w = _fraction_from(float(m.group(2)), True)     # the regex requires a %
        per[el] = frac
        per_pct[el] = float(m.group(2))
    if per:
        model = source_terms.global_release(None, per_element=per)
        desc = "Release model set: per-element fractions (" + ", ".join(
            f"{el} {_fmt_num(p)}%" for el, p in per_pct.items()) + ")"
        return ReleaseParse(model=model, is_global=False, per_element_pct=per_pct, description=desc)

    # Otherwise a single global fraction.
    for rx in (_GLOBAL_RELEASE_RE, _GLOBAL_RELEASE_RE2):
        m = rx.search(low)
        if not m:
            continue
        value = float(m.group(1))
        has_pct = bool(m.group(2))
        frac, warns = _fraction_from(value, has_pct)
        pct = round(frac * 100.0, 6)
        model = source_terms.global_release(frac)
        desc = f"Release model set: global {_fmt_num(pct)}% release fraction"
        return ReleaseParse(model=model, is_global=True, global_pct=pct, description=desc,
                            warnings=warns)
    return None


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
_DB_DAT_RE = re.compile(r"\b([a-z0-9_+\-]+\.dat)\b", re.I)


def parse_database(text: str) -> DatabaseParse | None:
    """Extract a database the user named (e.g. ``phreeqc.dat``), or ``None``. Never a fake path."""
    s = str(text or "")
    m = _DB_DAT_RE.search(s)
    if m:
        return DatabaseParse(name=m.group(1))
    low = s.lower()
    if "cemdata" in low:
        return DatabaseParse(name="CEMDATA18",
                             warnings=["CEMDATA18 is not redistributable — the server must provide "
                                       "it (configure PHREEQC_DATABASE)."])
    if re.search(r"\bphreeqc\b.{0,24}\bdatabase\b", low) or \
            re.search(r"\bdatabase\b.{0,24}\bphreeqc\b", low):
        return DatabaseParse(name="phreeqc.dat")
    return None


# --------------------------------------------------------------------------- #
# Confirmation intent (chat-side confirm of the parsed composition / release)
# --------------------------------------------------------------------------- #
_CONFIRM_RE = re.compile(
    r"\b(i\s+)?(confirm|approve|accept|looks?\s+good|that'?s?\s+(right|correct)|use\s+(this|that|it))\b",
    re.I)
_CONFIRM_SUBJECTS = ("composition", "profile", "release", "assay", "material", "oxide",
                     "numbers", "values")


def detect_confirmation(text: str) -> bool:
    """True for an explicit composition/release confirmation (needs a confirm verb AND a subject).

    Requiring a subject word keeps this distinct from a bare "yes"/"confirm" reply to a *parked run*
    (handled by the orchestrator's affirmation gate) — so confirming a composition can never be
    mistaken for confirming a PHREEQC execution.
    """
    low = str(text or "").lower()
    if not _CONFIRM_RE.search(low):
        return False
    return any(w in low for w in _CONFIRM_SUBJECTS)


# --------------------------------------------------------------------------- #
# Top-level extraction + apply-to-state
# --------------------------------------------------------------------------- #
def extract_setup(text: str) -> ChatSetup:
    """Parse a message for composition / release / database / a confirmation intent (pure)."""
    return ChatSetup(
        composition=parse_oxide_composition(text),
        release=parse_release(text),
        database=parse_database(text),
        confirm_requested=detect_confirmation(text),
    )


def _same_entries(profile, entries) -> bool:
    """True if a profile already carries exactly these (species, value) components."""
    if profile is None:
        return False
    try:
        existing = {(str(e.species).lower(), round(float(e.value), 4)) for e in profile.entries}
    except Exception:                                          # noqa: BLE001 - defensive
        return False
    incoming = {(str(e.species).lower(), round(float(e.value), 4)) for e in entries}
    return existing == incoming


def apply_setup(state, message, *, default_material_name=None) -> list[str]:
    """Apply a chat message's composition / release / database / confirm intent to ``state``.

    Mutates the **canonical** agent state (``state.material_profile`` / ``state.release_model`` /
    ``state.requested_database``) so the deterministic PHREEQC builder — which reads from the same
    state — sees the user's typed composition as an unconfirmed DRAFT. Returns human-readable notes
    for the assistant's reply. Safety: a new composition is always a DRAFT; a re-statement of an
    already-confirmed composition stays confirmed; a confirmation only flips a draft usable when it
    *validates*; release fractions are flagged as user assumptions; nothing is ever auto-run.
    """
    setup = extract_setup(message)
    notes: list[str] = []

    # 1) Composition (the user's literal numbers → a DRAFT profile). -------------------- #
    if setup.composition is not None:
        cp = setup.composition
        existing = getattr(state, "material_profile", None)
        pid = getattr(existing, "profile_id", None) or ASSISTANT_PROFILE_ID
        name = (default_material_name or getattr(existing, "material_name", None)
                or DEFAULT_MATERIAL_NAME)
        # Preserve a confirmed status only when the same composition is being re-stated.
        if _same_entries(existing, cp.entries) and getattr(existing, "is_usable", False):
            status = existing.verification_status
        else:
            status = mp.STATUS_DRAFT
        profile = mp.MaterialProfile(
            profile_id=pid, material_name=name, composition_basis=cp.basis,
            entries=cp.entries, source=mp.CompositionSource(source_type=mp.SOURCE_USER_ENTERED,
                                                            source_reference="assistant chat"),
            verification_status=status)
        state.material_profile = profile
        if status == mp.STATUS_DRAFT:
            notes.append(
                f"📋 I read your composition ({len(cp.entries)} components, total ≈"
                f"{cp.total_pct:g} wt%) into a **draft** in *Advanced details → Material "
                "composition*. **Review and confirm it** (I never use a composition you haven't "
                "confirmed).")
        for w in cp.warnings:
            notes.append(f"⚠️ {w}")
        for b in cp.blocking:
            notes.append(f"⛔ {b}")

    # 2) Release model (always a user assumption). -------------------------------------- #
    if setup.release is not None and setup.release.model is not None:
        state.release_model = setup.release.model
        notes.append(setup.release.description
                     + " — this is *your assumption*, not measured truth.")
        for w in setup.release.warnings:
            notes.append(f"⚠️ {w}")

    # 3) Database selection (a name, never a fabricated path). -------------------------- #
    if setup.database is not None and setup.database.name:
        try:
            state.requested_database = setup.database.name
        except Exception:                                      # noqa: BLE001 - defensive
            pass
        notes.append(f"🗄️ Database selected: **{setup.database.name}** (the app uses the "
                     "server-configured database; set it in Settings).")
        for w in setup.database.warnings:
            notes.append(f"⚠️ {w}")

    # 4) Confirmation intent (flip a draft → confirmed, but only if it validates). ------ #
    if setup.confirm_requested:
        prof = getattr(state, "material_profile", None)
        if prof is None:
            notes.append("I don't have a parsed composition to confirm yet — paste or type the "
                         "oxide composition first.")
        elif getattr(prof, "is_usable", False):
            notes.append("✅ Composition already confirmed.")
        else:
            res = profile_validation.validate_profile(prof)
            blockers = list(res.errors) + _impossible_values(prof)
            if res.can_confirm and not blockers:
                prof.verification_status = mp.STATUS_USER_CONFIRMED
                notes.append("✅ Composition confirmed — it can now feed the PHREEQC input preview.")
            else:
                notes.append("⛔ I can't confirm the composition yet: "
                             + "; ".join(blockers[:2]) + " Please fix it first.")

    return notes


def _impossible_values(profile) -> list[str]:
    """Per-entry blockers the status validator only *warns* on (a single value <0 or >100 wt%).

    ``profile_validation`` errors on negatives but only warns on >100 %; for the confirmation gate we
    treat an impossible single value (negative or above 100 wt%) as a hard blocker, never confirmable.
    """
    out: list[str] = []
    for entry in getattr(profile, "entries", []) or []:
        v = entry.numeric_value() if hasattr(entry, "numeric_value") else None
        if v is None:
            continue
        if v < 0:
            out.append(f"{entry.species} = {_fmt_num(v)} is negative")
        elif v > 100:
            out.append(f"{entry.species} = {_fmt_num(v)} wt% exceeds 100 %")
    return out
