"""Deterministic PHREEQC **input-preview** builder (no execution, no AI).

Turns a *confirmed* :class:`SimulationScenario` (or a simulation-matrix row) into a
reviewable, draft ``.pqi`` text. It is rule-based and testable — **AI never writes PHREEQC
input**: the LLM only extracts the scenario the user reviews; this module deterministically
templates the input from that scenario.

Hard boundaries (all enforced here / pinned by tests):

* It **never runs PHREEQC** — no subprocess, and it does not import :mod:`phreeqc_runner`.
* It **writes no files** — the caller downloads the text in-memory only.
* It is **off the scientific result path** (no mapping / residual / validation / comparison).
* It **imports no AI module**.
* It **never invents material composition** — a material's elemental assay is included only
  from a profile's *usable* declared assay (``measured`` / ``literature-confirmed``);
  otherwise the draft is clearly marked ``needs_material_composition`` and the composition is
  left as a labelled placeholder.

Conservative first-pass templates: **water**, **NaOH**, **HCl** leaching. Everything is a
DRAFT — the generated text carries explicit "PHREEQC has not been run / requires expert
review" comments.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import config, profiles
from . import scenario_schema as S
from .scenario_schema import SimulationScenario

# --------------------------------------------------------------------------- #
# Template kinds + statuses
# --------------------------------------------------------------------------- #
TEMPLATE_WATER = "water"
TEMPLATE_NAOH = "naoh"
TEMPLATE_HCL = "hcl"
TEMPLATE_UNSUPPORTED = "unsupported"
SUPPORTED_TEMPLATES = (TEMPLATE_WATER, TEMPLATE_NAOH, TEMPLATE_HCL)

STATUS_READY = "ready_for_review"
STATUS_DRAFT = "draft_only"
STATUS_NEEDS_COMPOSITION = "needs_material_composition"
STATUS_UNSUPPORTED_LEACHANT = "unsupported_leachant"
STATUS_MISSING_FIELD = "missing_required_field"
STATUS_TEMPLATE_WARNING = "template_warning"

# Leachant name (lower-cased) → template kind.
_LEACHANT_KIND = {
    "water": TEMPLATE_WATER, "di water": TEMPLATE_WATER, "deionized water": TEMPLATE_WATER,
    "deionised water": TEMPLATE_WATER, "distilled water": TEMPLATE_WATER, "h2o": TEMPLATE_WATER,
    "milliq": TEMPLATE_WATER, "milli-q": TEMPLATE_WATER, "ultrapure water": TEMPLATE_WATER,
    "naoh": TEMPLATE_NAOH, "sodium hydroxide": TEMPLATE_NAOH,
    "hcl": TEMPLATE_HCL, "hydrochloric acid": TEMPLATE_HCL, "hydrochloric": TEMPLATE_HCL,
}

# Leachants the on-demand RUNNER can actually template/execute (NaOH only, today).
RUNNER_SUPPORTED_LEACHANTS = tuple(s.lower() for s in S.TEMPLATE_SUPPORTED_LEACHANTS)  # ("naoh",)

# The label the UI must show on the preview.
PREVIEW_HEADER_LABEL = "Input preview only — PHREEQC has not been run yet."

# The mandatory disclaimers, written as PHREEQC comments in every generated input.
PREVIEW_DISCLAIMERS = [
    "PHREEQC INPUT PREVIEW — DRAFT ONLY. PHREEQC has NOT been run; this is not a result.",
    "Requires expert review before any simulation is run.",
    "The thermodynamic DATABASE choice (e.g. CEMDATA18) materially affects the predictions.",
    "Material composition / profile controls prediction quality — a draft without a real "
    "assay is not scientifically meaningful.",
    "Kinetic dissolution is NOT represented unless a KINETICS/RATES block is explicitly "
    "added (this draft is equilibrium-only).",
    "Precipitation predictions depend entirely on the selected EQUILIBRIUM_PHASES and the "
    "database.",
    "This text is deterministic, rule-based output — AI did not write it; AI only extracted "
    "the scenario you reviewed.",
]


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass
class PhreeqcInputPreview:
    scenario_id: str
    phreeqc_input_text: str
    template_type: str
    status: str
    warnings: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    unsupported_features: list = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return self.status == STATUS_READY

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "template_type": self.template_type,
            "status": self.status,
            "warnings": list(self.warnings),
            "assumptions": list(self.assumptions),
            "unsupported_features": list(self.unsupported_features),
            "phreeqc_input_text": self.phreeqc_input_text,
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _fmt(x, nd: int = 4) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    if f == int(f):
        return str(int(f))
    return f"{f:.{nd}g}"


def leachant_kind(leachant) -> str:
    """Map a leachant name to a supported template kind, or ``unsupported``."""
    s = str(leachant or "").strip().lower()
    if not s:
        return TEMPLATE_UNSUPPORTED
    if s in _LEACHANT_KIND:
        return _LEACHANT_KIND[s]
    for needle, kind in _LEACHANT_KIND.items():
        if needle in s:
            return kind
    return TEMPLATE_UNSUPPORTED


def resolve_material_profile(scenario: SimulationScenario, default=None):
    """Best-effort match of the scenario's material to a known MaterialProfile, else
    ``default``. Pure name matching — never invents composition."""
    name = " ".join(filter(None, [scenario.material.material_type,
                                   scenario.material.material_name])).lower()
    if any(t in name for t in ("fly ash", "fly_ash", "flyash", "cfa")):
        return profiles.FLY_ASH_MATERIAL
    if any(t in name for t in ("red mud", "red_mud", "bauxite")):
        return profiles.RED_MUD_MATERIAL
    return default


def _usable_composition(material_profile) -> dict:
    """``{element: AssayValue}`` from the profile's **usable** declared assay only.

    Never invents values: a ``literature-proposed`` (quarantined) or absent assay
    contributes nothing.
    """
    if material_profile is None:
        return {}
    out: dict = {}
    for el in getattr(material_profile, "relevant_elements", ()) or ():
        av = None
        try:
            av = material_profile.usable_assay(el)
        except Exception:
            av = None
        if av is not None:
            out[el] = av
    return out


def _required_field_issues(scenario: SimulationScenario, kind: str) -> list[str]:
    """Hard-required scenario fields that are missing (validation, section 6)."""
    issues: list[str] = []
    if not (scenario.material.material_name or scenario.material.material_type):
        issues.append("material name/type")
    if scenario.material.solid_mass_g is None:
        issues.append("solid mass (g)")
    if scenario.leachant.liquid_volume_mL is None:
        issues.append("liquid volume (mL)")
    if not scenario.leachant.leachant_type:
        issues.append("leachant type")
    if kind != TEMPLATE_WATER and scenario.leachant.leachant_concentration_M is None:
        issues.append("leachant concentration (M)")
    if scenario.process.time_min is None:
        issues.append("reaction time (min)")
    return issues


def _target_elements(scenario: SimulationScenario) -> list[str]:
    els = [e for e in (scenario.outputs.target_elements or []) if e in S.RECOGNIZED_ELEMENTS]
    return els or list(config.RESIDUAL_ELEMENTS)


def _composition_provenance_lines(material_profile) -> list[str]:
    """Optional PHREEQC comment lines for the composition's *basis / source / status*.

    Read via ``getattr`` so a frozen :class:`profiles.MaterialProfile` (which has none of
    these attributes) contributes **no lines** — the generated text for fly ash / red mud is
    byte-for-byte unchanged. A user :class:`materials.MaterialProfile` exposes them, so its
    basis + source + verification status land in the input comments (requested behaviour).
    """
    lines: list[str] = []
    basis = getattr(material_profile, "composition_basis", None)
    if basis:
        label = None
        try:                                    # human label if the profile offers one
            label = material_profile.basis_label()
        except Exception:
            label = None
        lines.append(f"#   composition basis:  {label or basis}")
    vstatus = getattr(material_profile, "verification_status", None)
    if vstatus:
        lines.append(f"#   verification:        {vstatus}")
    src = getattr(material_profile, "source", None)
    if src is not None:
        st_type = getattr(src, "source_type", None)
        ref = getattr(src, "source_reference", None)
        if st_type:
            lines.append(f"#   composition source:  {st_type}"
                         + (f" — {ref}" if ref else ""))
        cite = getattr(src, "citation", None)
        if cite:
            title = getattr(src, "title", None)
            lines.append(f"#   source citation:     {cite}"
                         + (f" ({title})" if title else ""))
    return lines


# --------------------------------------------------------------------------- #
# Leachant SOLUTION blocks (deterministic; assumptions stated)
# --------------------------------------------------------------------------- #
def _solution_block(kind: str, scenario: SimulationScenario, temp: float,
                    assumptions: list[str]) -> list[str]:
    conc = scenario.leachant.leachant_concentration_M
    lines: list[str] = []
    if kind == TEMPLATE_WATER:
        lines += [
            "SOLUTION 1  Leachant: deionized / neutral water (no molarity)",
            f"    temp      {_fmt(temp, 2)}",
            "    pH        7.0       # neutral DI water (ASSUMPTION)",
            "    units     mol/kgw",
            "    -water    1         # kg",
        ]
        assumptions.append("DI/neutral water assumed at pH 7 (no molarity)")
    elif kind == TEMPLATE_NAOH:
        lines += [
            f"SOLUTION 1  Leachant: NaOH {_fmt(conc)} M",
            f"    temp      {_fmt(temp, 2)}",
            "    units     mol/kgw",
            f"    Na        {_fmt(conc)}        # mol/L from NaOH (ASSUMES full dissociation; "
            "density ~1 so mol/L ~ mol/kgw)",
            "    pH        13.0  charge   # high pH from NaOH; pH used for charge balance "
            "(ASSUMPTION)",
        ]
        assumptions += [
            f"Na set to the NaOH molarity ({_fmt(conc)} mol/L); NaOH assumed fully dissociated",
            "charge balanced on pH (≈13); ionic-strength / activity not separately corrected",
        ]
    elif kind == TEMPLATE_HCL:
        lines += [
            f"SOLUTION 1  Leachant: HCl {_fmt(conc)} M",
            f"    temp      {_fmt(temp, 2)}",
            "    units     mol/kgw",
            f"    Cl        {_fmt(conc)}        # mol/L from HCl (ASSUMES full dissociation)",
            "    pH        1.0   charge   # low pH from HCl; pH used for charge balance "
            "(ASSUMPTION)",
        ]
        assumptions += [
            f"Cl set to the HCl molarity ({_fmt(conc)} mol/L); HCl assumed fully dissociated",
            "charge balanced on pH (≈1); ionic-strength / activity not separately corrected",
        ]
    else:  # unsupported
        lt = scenario.leachant.leachant_type or "unknown"
        lines += [
            f"SOLUTION 1  Leachant: {lt} — UNSUPPORTED for templating",
            f"    temp      {_fmt(temp, 2)}",
            "    pH        7.0   charge   # placeholder — no preview template for this leachant",
            "    units     mol/kgw",
            f"#   The leachant '{lt}' is not one of the supported preview templates "
            "(water / NaOH / HCl).",
            "#   Define the leachant chemistry manually before running.",
        ]
    return lines


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_phreeqc_input_preview(scenario: SimulationScenario, *,
                                scenario_id: str = "SIM-001",
                                material_profile=None) -> PhreeqcInputPreview:
    """Build a deterministic, draft PHREEQC input preview for one confirmed scenario.

    ``material_profile`` is an optional :class:`MaterialProfile`; if ``None`` it is
    best-effort resolved from the scenario's material name (fly ash / red mud / generic).
    Never raises on missing data — it returns a labelled draft + warnings instead.
    """
    warnings: list[str] = []
    assumptions: list[str] = []
    unsupported: list[str] = []

    kind = leachant_kind(scenario.leachant.leachant_type)
    if material_profile is None:
        material_profile = resolve_material_profile(scenario)

    # --- temperature (assume ambient if missing) -------------------------- #
    temp = scenario.process.temperature_C
    if temp is None:
        temp = S.ASSUMED_TEMPERATURE_C
        assumptions.append(f"temperature assumed {_fmt(S.ASSUMED_TEMPERATURE_C)} °C "
                           "(no explicit value)")

    # --- validation + composition ----------------------------------------- #
    missing = _required_field_issues(scenario, kind)
    if missing:
        warnings.append("Missing required field(s): " + ", ".join(missing)
                        + " — the draft below uses placeholders for them.")
    composition = _usable_composition(material_profile)
    composition_available = bool(composition)
    if not composition_available:
        warnings.append(
            "Material composition is not available as an approved profile assay — a "
            "meaningful PHREEQC prediction requires a measured or literature-confirmed "
            "material composition. The draft is structural only.")
        unsupported.append("dissolved material composition (no usable declared assay)")

    if kind == TEMPLATE_UNSUPPORTED:
        warnings.append(
            f"Unsupported leachant '{scenario.leachant.leachant_type}' — no preview template "
            "(supported: water, NaOH, HCl). A generic placeholder solution was emitted.")
        unsupported.append(f"leachant template for '{scenario.leachant.leachant_type}'")
    elif kind.upper() not in (s.upper() for s in RUNNER_SUPPORTED_LEACHANTS):
        # water / HCl: the on-demand runner currently templates NaOH only.
        warnings.append(
            f"'{kind}' is preview-only — the on-demand PHREEQC runner currently templates "
            "NaOH activation only, so this input is **not validated against the runner**.")
        unsupported.append(f"runner execution for a '{kind}' leachant (runner is NaOH-only)")

    # time → kinetics caveat (equilibrium-only draft)
    if scenario.process.time_min is not None:
        unsupported.append(
            f"reaction time ({_fmt(scenario.process.time_min)} min) — equilibrium draft does "
            "not model kinetic dissolution (add a KINETICS/RATES block to use it)")

    # --- status (deterministic precedence) -------------------------------- #
    if kind == TEMPLATE_UNSUPPORTED:
        status = STATUS_UNSUPPORTED_LEACHANT
    elif missing:
        status = STATUS_MISSING_FIELD
    elif composition_available:
        status = STATUS_READY if kind == TEMPLATE_NAOH else STATUS_TEMPLATE_WARNING
    elif material_profile is not None:
        status = STATUS_NEEDS_COMPOSITION
    else:
        status = STATUS_DRAFT

    text = _assemble_text(scenario, scenario_id, kind, temp, composition, material_profile,
                          assumptions, warnings, unsupported, status)
    return PhreeqcInputPreview(
        scenario_id=scenario_id, phreeqc_input_text=text, template_type=kind, status=status,
        warnings=warnings, assumptions=assumptions, unsupported_features=unsupported)


def _assemble_text(scenario, scenario_id, kind, temp, composition, material_profile,
                   assumptions, warnings, unsupported, status) -> str:
    flat = scenario.to_flat_dict()
    material = flat.get("material_name") or flat.get("material_type") or "unspecified"
    targets = _target_elements(scenario)
    lines: list[str] = []

    # 1) preview disclaimers (mandatory comments)
    lines.append(f"# ===== {PREVIEW_HEADER_LABEL} =====")
    for d in PREVIEW_DISCLAIMERS:
        lines.append(f"# {d}")
    lines.append(f"# status: {status}")
    lines.append("# DATABASE is intentionally omitted — supply CEMDATA18 (or another database) "
                 "to the PHREEQC CLI at run time.")
    lines.append("")

    # 2) scenario metadata comments
    lines.append(f"# --- scenario {scenario_id} (metadata, not run) ---")
    lines.append(f"#   material:            {material}")
    lines.append(f"#   leachant:            {flat.get('leachant_type')} "
                 f"({'water — no molarity' if kind == TEMPLATE_WATER else str(flat.get('leachant_concentration_M')) + ' M'})")
    lines.append(f"#   solid mass (g):      {flat.get('solid_mass_g')}")
    lines.append(f"#   liquid volume (mL):  {flat.get('liquid_volume_mL')}")
    lines.append(f"#   L/S ratio:           {flat.get('liquid_solid_ratio')}")
    lines.append(f"#   reaction time (min): {flat.get('time_min')}")
    lines.append(f"#   temperature (°C):    {_fmt(temp)}")
    lines.append(f"#   target outputs:      {', '.join(targets)} (+ pH)")
    if assumptions:
        lines.append("#   assumptions:")
        for a in assumptions:
            lines.append(f"#     - {a}")
    if warnings:
        lines.append("#   warnings:")
        for w in warnings:
            lines.append(f"#     - {w}")
    lines.append("")

    # 3) TITLE + leachant SOLUTION
    lines.append(f"TITLE {scenario_id} — DRAFT preview ({kind} leaching of {material})")
    lines.append("")
    lines += _solution_block(kind, scenario, temp, assumptions)
    lines.append("")

    # 4) dissolved material composition (only from a usable assay; never invented)
    lines.append("# --- dissolved material composition ---")
    if composition:
        name = getattr(material_profile, "display_name", None) or material
        lines.append(f"# From the usable declared assay of '{name}' (review the dissolution "
                     "assumption — this lists the BULK assay, not a computed dissolved amount):")
        lines += _composition_provenance_lines(material_profile)
        for el, av in composition.items():
            lines.append(f"#   {el} = {_fmt(getattr(av, 'value', av))} "
                         f"{getattr(av, 'unit', '')} ({getattr(av, 'provenance', 'declared')})")
        lines.append("# Convert the bulk assay to dissolved mol/L (with a dissolution model) "
                     "before running — this draft does not assume a dissolution extent.")
    else:
        lines.append("# NOT INCLUDED — no usable measured/literature-confirmed material assay is "
                     "available.")
        lines.append("# A meaningful prediction REQUIRES the dissolved material composition. "
                     "Supply it from a measured assay or a confirmed literature value, then add "
                     "the dissolved elements (mol/L) to SOLUTION 1 above.")
    lines.append("")

    # 5) candidate precipitate phases (from the material profile, if any) — draft
    phases = dict(getattr(material_profile, "candidate_phases", {}) or {}) \
        if material_profile is not None else {}
    lines.append("# --- candidate precipitate phases (EQUILIBRIUM_PHASES) ---")
    if phases:
        lines.append("EQUILIBRIUM_PHASES 1")
        for phase in phases:
            lines.append(f"    {phase}    0   0   # allowed to precipitate (draft — verify "
                         "against the database)")
    else:
        lines.append("# No candidate phases declared for this material — define the precipitate "
                     "phases to model (predictions depend entirely on this list + the database).")
    lines.append("")

    # 6) SELECTED_OUTPUT for the requested variables
    lines.append("SELECTED_OUTPUT")
    lines.append("    -pH        true")
    lines.append(f"    -totals    {' '.join(targets)}")
    lines.append("")
    lines.append("END")
    lines.append("")
    lines.append(f"# {PREVIEW_HEADER_LABEL}")
    return "\n".join(lines)


def build_previews_for_matrix(scenario: SimulationScenario, matrix_df, *,
                              material_profile=None) -> list[PhreeqcInputPreview]:
    """One preview per simulation-matrix row (handles single scenario + parameter sweeps).

    Each row's swept values (concentration / time / temperature / L:S) override the base
    confirmed scenario; the row's ``scenario_id`` labels the preview.
    """
    from . import matrix as _matrix
    base = scenario.to_flat_dict()
    swept = [c for c in _matrix.RANGEABLE_FIELDS if c in getattr(matrix_df, "columns", [])]
    out: list[PhreeqcInputPreview] = []
    rows = matrix_df.to_dict("records") if hasattr(matrix_df, "to_dict") else list(matrix_df)
    for i, row in enumerate(rows, start=1):
        flat = dict(base)
        for f in swept:
            if row.get(f) is not None:
                flat[f] = row.get(f)
        sc = SimulationScenario.from_flat_dict(flat)
        sid = str(row.get("scenario_id") or f"SIM-{i:03d}")
        out.append(build_phreeqc_input_preview(sc, scenario_id=sid, material_profile=material_profile))
    return out
