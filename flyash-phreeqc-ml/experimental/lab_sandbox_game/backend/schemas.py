"""Material Card schema + lab-station vocabulary for the 2D Lab Sandbox (EXPERIMENTAL).

This module is the **honesty contract** of the sandbox backend. It is pure data + validation:
no AI, no execution, no network. It owns the one vocabulary every route and the game client agree
on, so a card can never travel without saying *what kind of data it is* and *which lab stations it
is honestly eligible for*.

The hard rules it encodes (mirroring the main platform's scientific rules):

* Every card carries a ``data_status`` from a fixed vocabulary — measured / simulated / predicted /
  reference / synthetic_demo / assumed / cached / unknown / missing, plus the honest scaffold
  extensions ``formula_only`` (stoichiometry parsed, phases/structure NOT known) and
  ``user_provided``. Nothing is ever silently "real".
* ``structure_source`` says whether a crystal structure / phase identity is actually known
  (``reference_database`` / ``user_supplied``) or not (``none``). XRD eligibility depends on it.
* ``allowed_lab_stations`` is computed deterministically from what data is *present* — it lists every
  station with an ``eligible`` flag and a plain-language ``reason``, including the ones you *cannot*
  use yet and why. It never grants XRD on a formula alone, and ICP is never satisfied by a solid card.

The companion language-agnostic contract is ``../material_card_schema.json`` (the game client reads
that); :func:`validate_material_card` enforces the same rules in Python and a test checks they agree.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = "0.0.1-scaffold"

# --------------------------------------------------------------------------- #
# data_status — the epistemic status of a card's data. NEVER omit it.
# --------------------------------------------------------------------------- #
MEASURED = "measured"               # real laboratory measurement (the sandbox never invents this)
SIMULATED = "simulated"             # output of a physical simulation (e.g. PHREEQC) — not measured
PREDICTED = "predicted"             # output of a trained/ML surrogate — not measured
REFERENCE = "reference"             # from a reference database (known phase / crystal structure)
SYNTHETIC_DEMO = "synthetic_demo"   # a labelled synthetic demo composition (e.g. the demo fly ash)
ASSUMED = "assumed"                 # an assumption (e.g. a user-typed composition taken as given)
CACHED = "cached"                   # a previously computed result served from cache
UNKNOWN = "unknown"                 # could not be resolved; no data was invented around it
MISSING = "missing"                 # a required input is absent
FORMULA_ONLY = "formula_only"       # stoichiometry parsed, but phases / crystal structure NOT known
USER_PROVIDED = "user_provided"     # supplied verbatim by the user (treated as assumed until checked)

DATA_STATUSES = frozenset({
    MEASURED, SIMULATED, PREDICTED, REFERENCE, SYNTHETIC_DEMO, ASSUMED, CACHED,
    UNKNOWN, MISSING, FORMULA_ONLY, USER_PROVIDED,
})

# Short human labels for the game HUD (the client may override styling, but not meaning).
DATA_STATUS_LABELS = {
    MEASURED: "Measured", SIMULATED: "Simulated", PREDICTED: "Predicted",
    REFERENCE: "Reference", SYNTHETIC_DEMO: "Synthetic demo", ASSUMED: "Assumed",
    CACHED: "Cached", UNKNOWN: "Unknown", MISSING: "Missing",
    FORMULA_ONLY: "Formula only", USER_PROVIDED: "User-provided",
}

# --------------------------------------------------------------------------- #
# structure_source — is a crystal structure / phase identity actually known?
# --------------------------------------------------------------------------- #
STRUCT_REFERENCE_DB = "reference_database"   # a known phase/structure from an internal reference
STRUCT_USER_SUPPLIED = "user_supplied"       # the user supplied a phase/structure
STRUCT_NONE = "none"                         # no structure known (formula/composition only)

STRUCTURE_SOURCES = frozenset({STRUCT_REFERENCE_DB, STRUCT_USER_SUPPLIED, STRUCT_NONE})

# A crystal-structure-bearing source is required before XRD can plan expected peaks.
_STRUCT_WITH_PHASE = frozenset({STRUCT_REFERENCE_DB, STRUCT_USER_SUPPLIED})

# --------------------------------------------------------------------------- #
# Lab stations — the four Phase-1 station concepts. Honest "kind" + what they refuse.
# --------------------------------------------------------------------------- #
STATION_SYNTHESIZER = "synthesizer"
STATION_XRD = "xrd"
STATION_PHREEQC = "phreeqc"
STATION_ICP = "icp"

STATIONS = {
    STATION_SYNTHESIZER: {
        "station_id": STATION_SYNTHESIZER,
        "display_name": "Synthesizer",
        "kind": "material_intake",
        "consumes": "a material name, chemical formula, or a user-typed composition",
        "produces": "a Material Card with an honest data_status and warnings",
        "refuses": "inventing phases or a crystal structure for an unknown material",
    },
    STATION_XRD: {
        "station_id": STATION_XRD,
        "display_name": "XRD Station",
        "kind": "signal_advisory",          # expected peaks from known phases — never identification
        "consumes": "a card whose phases have a known structure, or an explicit phase list",
        "produces": "EXPECTED / approximate reference peaks (a measurement plan)",
        "refuses": "an exact pattern from a formula alone, and any 'measured/identified' claim",
    },
    STATION_PHREEQC: {
        "station_id": STATION_PHREEQC,
        "display_name": "PHREEQC Station",
        "kind": "physical_simulation",      # gated: preview, then run only on explicit confirmation
        "consumes": "a confirmed composition + source term + leachant + database",
        "produces": "a PHREEQC input PREVIEW (and, only behind the confirm gate, a real run)",
        "refuses": "auto-running; it previews first and executes only after explicit confirmation",
        "gated": True,
    },
    STATION_ICP: {
        "station_id": STATION_ICP,
        "display_name": "ICP Processor Station",
        "kind": "data_processing",          # reduces measured/predicted solution data; no plasma sim
        "consumes": "a measured or predicted SOLUTION concentration table you provide",
        "produces": "unit conversions, dilution/blank correction, and measured-vs-predicted residuals",
        "refuses": "simulating the plasma, and fabricating measured values from a solid composition",
    },
}
STATION_IDS = tuple(STATIONS.keys())


# --------------------------------------------------------------------------- #
# Composition basis — how composition values should be read.
# --------------------------------------------------------------------------- #
BASIS_OXIDE_WT_PCT = "oxide_wt_percent"     # e.g. the demo fly ash assay (SiO2 34, CaO 24, ...)
BASIS_ELEMENT_MOL = "element_mol_ratio"     # element counts from a parsed formula
BASIS_ELEMENT_WT_PCT = "element_wt_percent"
BASIS_CONCENTRATION = "solution_concentration"
COMPOSITION_BASES = frozenset({
    BASIS_OXIDE_WT_PCT, BASIS_ELEMENT_MOL, BASIS_ELEMENT_WT_PCT, BASIS_CONCENTRATION,
})


def station_eligibility(*, phases, structure_source, composition) -> list[dict]:
    """Deterministically decide which stations a card is honestly eligible for, with reasons.

    Returns one entry per station: ``{station_id, display_name, eligible, reason}``. The rules are
    the honesty gates:

    * **Synthesizer** — always eligible (it is where cards come from).
    * **XRD** — eligible only when the card has at least one phase *and* a real structure source
      (``reference_database`` / ``user_supplied``). A formula/composition alone is never enough.
    * **PHREEQC** — eligible to build a *preview* once a composition is present. (Eligibility is not
      permission to run: execution always stays behind the confirmation gate at the PHREEQC station.)
    * **ICP** — never satisfied by a solid card: ICP reduces a measured/predicted *solution* table.
    """
    has_phases = bool(phases)
    has_structure = structure_source in _STRUCT_WITH_PHASE
    has_composition = bool(composition and (composition.get("values") if isinstance(composition, dict) else composition))

    entries: list[dict] = []

    def add(station_id, eligible, reason):
        entries.append({
            "station_id": station_id,
            "display_name": STATIONS[station_id]["display_name"],
            "eligible": bool(eligible),
            "reason": reason,
        })

    add(STATION_SYNTHESIZER, True, "Origin station — every material enters the lab here.")

    if has_phases and has_structure:
        add(STATION_XRD, True,
            "Phases with a known structure are present — XRD can plan EXPECTED (approximate) peaks. "
            "This is a measurement plan, never an identification.")
    else:
        missing = "phase identity + a reference/known crystal structure"
        add(STATION_XRD, False,
            f"XRD needs {missing}; a formula or composition alone cannot yield an exact pattern.")

    if has_composition:
        add(STATION_PHREEQC, True,
            "A composition is present, so a PHREEQC input PREVIEW can be built. Execution still "
            "requires the leachant + source term + database and explicit confirmation at the station.")
    else:
        add(STATION_PHREEQC, False,
            "PHREEQC needs a composition (plus leachant, source term, database) before even a preview.")

    add(STATION_ICP, False,
        "ICP reduces a MEASURED or PREDICTED solution concentration table that you provide; a solid "
        "material card is not ICP input and is never turned into fabricated measured values.")

    return entries


# --------------------------------------------------------------------------- #
# Material Card
# --------------------------------------------------------------------------- #
@dataclass
class MaterialCard:
    """One inventory item the player carries between stations. Self-describing and honest.

    ``phases`` is a list of ``{name, formula, source, note}`` and is **empty** unless a structure is
    genuinely known. ``composition`` is ``{basis, values, status, note}`` or ``None``.
    ``allowed_lab_stations`` is computed by :func:`station_eligibility`. ``data_status`` is the
    single most-important field: it is the card's epistemic label and must be from
    :data:`DATA_STATUSES`.
    """

    material_id: str
    display_name: str
    data_status: str
    formula: str | None = None
    phases: list = field(default_factory=list)
    composition: dict | None = None
    structure_source: str = STRUCT_NONE
    provenance: str = ""
    uncertainty_notes: list = field(default_factory=list)
    allowed_lab_stations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """A JSON-safe view (the exact shape the API returns and the game client consumes)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "material_id": self.material_id,
            "display_name": self.display_name,
            "data_status": self.data_status,
            "data_status_label": DATA_STATUS_LABELS.get(self.data_status, self.data_status),
            "formula": self.formula,
            "phases": list(self.phases),
            "composition": self.composition,
            "structure_source": self.structure_source,
            "provenance": self.provenance,
            "uncertainty_notes": list(self.uncertainty_notes),
            "allowed_lab_stations": list(self.allowed_lab_stations),
            "warnings": list(self.warnings),
        }


# Fields a card dict must carry to be valid (used by validation + the schema test).
REQUIRED_CARD_FIELDS = (
    "material_id", "display_name", "data_status", "structure_source",
    "phases", "allowed_lab_stations", "warnings",
)


def validate_material_card(card) -> list[str]:
    """Return a list of schema/honesty problems for ``card`` (a dict). Empty == valid.

    Beyond shape, this enforces the cross-field honesty invariants so a card can never *claim* an
    eligibility its data does not support:

    * ``data_status`` ∈ :data:`DATA_STATUSES`, ``structure_source`` ∈ :data:`STRUCTURE_SOURCES`.
    * Every phase is a mapping with a non-empty ``name`` and a ``source``.
    * If a card has any phases, it must declare a real ``structure_source`` (not ``none``) — phases
      without a structure source would be an unsupported identification.
    * If ``allowed_lab_stations`` marks **XRD eligible**, the card MUST actually have phases + a real
      structure source. (No XRD-on-a-formula.)
    * If it marks **ICP eligible**, that is rejected — a solid card is never ICP input.
    * ``composition`` (when present) must declare a known ``basis``.
    """
    problems: list[str] = []
    if not isinstance(card, dict):
        return [f"card must be a dict, got {type(card).__name__}"]

    for f in REQUIRED_CARD_FIELDS:
        if f not in card:
            problems.append(f"missing required field {f!r}")

    mid = card.get("material_id")
    if not (isinstance(mid, str) and mid.strip()):
        problems.append("material_id must be a non-empty string")
    if not (isinstance(card.get("display_name"), str) and card.get("display_name", "").strip()):
        problems.append("display_name must be a non-empty string")

    status = card.get("data_status")
    if status not in DATA_STATUSES:
        problems.append(f"data_status {status!r} is not in the allowed vocabulary")

    struct = card.get("structure_source")
    if struct not in STRUCTURE_SOURCES:
        problems.append(f"structure_source {struct!r} is not in the allowed vocabulary")

    phases = card.get("phases")
    if not isinstance(phases, list):
        problems.append("phases must be a list")
        phases = []
    for i, ph in enumerate(phases):
        if not isinstance(ph, dict):
            problems.append(f"phases[{i}] must be a mapping")
            continue
        if not (isinstance(ph.get("name"), str) and ph.get("name", "").strip()):
            problems.append(f"phases[{i}] missing a non-empty 'name'")
        if not ph.get("source"):
            problems.append(f"phases[{i}] missing 'source' (where the phase identity came from)")

    # Honesty: phases present ⇒ a real structure source must be declared.
    if phases and struct not in _STRUCT_WITH_PHASE:
        problems.append("card lists phases but structure_source is not reference_database/user_supplied "
                        "— that would be an unsupported phase identification")

    comp = card.get("composition")
    if comp is not None:
        if not isinstance(comp, dict):
            problems.append("composition must be a mapping or null")
        elif comp.get("basis") not in COMPOSITION_BASES:
            problems.append(f"composition.basis {comp.get('basis')!r} is not a known basis")

    stations = card.get("allowed_lab_stations")
    if not isinstance(stations, list):
        problems.append("allowed_lab_stations must be a list")
        stations = []
    for i, st in enumerate(stations):
        if not isinstance(st, dict):
            problems.append(f"allowed_lab_stations[{i}] must be a mapping")
            continue
        sid = st.get("station_id")
        if sid not in STATIONS:
            problems.append(f"allowed_lab_stations[{i}] unknown station_id {sid!r}")
        if "eligible" not in st:
            problems.append(f"allowed_lab_stations[{i}] missing 'eligible'")
        if not st.get("reason"):
            problems.append(f"allowed_lab_stations[{i}] missing 'reason'")
        # Cross-field honesty gates.
        if sid == STATION_XRD and st.get("eligible") and not (phases and struct in _STRUCT_WITH_PHASE):
            problems.append("XRD is marked eligible but the card lacks phases + a known structure")
        if sid == STATION_ICP and st.get("eligible"):
            problems.append("ICP must never be eligible from a solid card (it needs a solution table)")

    for list_field in ("uncertainty_notes", "warnings"):
        if list_field in card and not isinstance(card[list_field], list):
            problems.append(f"{list_field} must be a list")

    return problems
