"""Structured scenario schema for the natural-language simulation planner.

Plain stdlib dataclasses (the project does not use pydantic). The schema captures *what a
batch-leaching experiment is* (material, leachant, process, target outputs), plus the
planner's bookkeeping (missing inputs, assumptions, warnings, confidence). It is pure data
+ serialization — no AI, no PHREEQC, no I/O.

The vocabulary (CO2 codes, elements, the assumed temperature) is sourced from
:mod:`flyash_phreeqc_ml.config` so the planner agrees with the rest of the app.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .. import config

# --------------------------------------------------------------------------- #
# Vocabulary (single source of truth = config where it exists)
# --------------------------------------------------------------------------- #
# Elements the planner recognises (core measured set + the project's extras).
RECOGNIZED_ELEMENTS = ("Ca", "Si", "Al", "Fe", "Na", "K", "Sc", "REE")

# CO2 cup-cover vocabulary (experiment + model codes) — reused verbatim from config.
CO2_CONDITION_ALLOWED = tuple(config.CO2_CONDITION_ALLOWED)
COVER_CONDITIONS = ("open_air", "plastic_flap", "glass_cover", "unknown")

# Leachants the current on-demand PHREEQC template supports (NaOH activation only;
# acid/water leaching has no CEMDATA NaOH/CO2 analogue — see phreeqc_runner).
TEMPLATE_SUPPORTED_LEACHANTS = ("NaOH",)

# What a simulation can be asked to produce.
DESIRED_OUTPUTS_VOCAB = ("liquid_composition", "precipitated_phases", "pH",
                         "saturation_indices", "mass_balance")

ASSUMED_TEMPERATURE_C = float(config.ASSUMED_TEMPERATURE_C)

# Source tags for a parse result.
SOURCE_AI = "ai"
SOURCE_RULE = "rule"
SOURCE_RULE_FALLBACK = "rule_fallback"   # AI failed/invalid → rule-based fallback used

# Severities for a missing input.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# --------------------------------------------------------------------------- #
# Standing caveats / labels (the honesty wording — single source)
# --------------------------------------------------------------------------- #
PLAN_ONLY_LABEL = ("Simulation plan only — no deterministic simulation has been run yet "
                   "(no model output generated).")
PRECIPITATION_CAVEAT = (
    "Precipitation or retention cannot be proven from liquid data alone. It requires "
    "PHREEQC phase predictions, solid residue data, or mass-balance assumptions."
)
NON_PREDICTION_NOTE = (
    "This is a simulation plan extracted from your description — not a scientific "
    "prediction. No deterministic simulation has been run, and nothing here changes any "
    "saved data, model predictions, or downstream comparison/validation results. (For the "
    "fly-ash + PHREEQC workflow, those downstream artifacts are the measured data, "
    "mappings, residuals, and validation status.)"
)


# --------------------------------------------------------------------------- #
# Coercion helpers (defensive — used by the parsers + from_flat_dict)
# --------------------------------------------------------------------------- #
def as_float(value):
    """A finite float, or ``None``."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def as_bool(value):
    """A tri-state bool: ``True`` / ``False`` / ``None`` (unknown)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


def as_str(value):
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def as_str_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).replace(";", ",").split(",")
    out = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# Input sub-structures
# --------------------------------------------------------------------------- #
@dataclass
class MaterialInput:
    material_name: str | None = None     # human label, e.g. "Class C fly ash"
    material_type: str | None = None     # machine type, e.g. "class_c_fly_ash"
    solid_mass_g: float | None = None


@dataclass
class LeachantInput:
    leachant_type: str | None = None             # "HCl" / "NaOH" / "water" / ...
    leachant_concentration_M: float | None = None
    liquid_volume_mL: float | None = None
    pH_initial: float | None = None


@dataclass
class ExperimentProcess:
    time_min: float | None = None
    temperature_C: float | None = None
    CO2_condition: str | None = None             # OA/PF/GS/atm_CO2/low_CO2/no_CO2
    cover_condition: str | None = None           # open_air/plastic_flap/glass_cover
    centrifuge_used: bool | None = None
    filtration_used: bool | None = None
    filter_size_um: float | None = None


@dataclass
class TargetOutputs:
    target_elements: list = field(default_factory=list)   # Ca, Si, Al, Fe, ...
    desired_outputs: list = field(default_factory=list)   # liquid_composition, ...
    notes: str | None = None


@dataclass
class MissingInput:
    field: str
    label: str
    severity: str = SEVERITY_WARNING
    message: str = ""


@dataclass
class Assumption:
    field: str
    assumed_value: object = None
    reason: str = ""
    source: str = "default"              # "ai" / "rule" / "default"


# --------------------------------------------------------------------------- #
# The scenario (composes the sub-structures; exposes a flat view for UI + matrix)
# --------------------------------------------------------------------------- #
@dataclass
class SimulationScenario:
    material: MaterialInput = field(default_factory=MaterialInput)
    leachant: LeachantInput = field(default_factory=LeachantInput)
    process: ExperimentProcess = field(default_factory=ExperimentProcess)
    outputs: TargetOutputs = field(default_factory=TargetOutputs)
    liquid_solid_ratio: float | None = None
    confidence: float = 0.0
    notes: str | None = None
    warnings: list = field(default_factory=list)

    def computed_ls_ratio(self):
        """Liquid/solid ratio (mL/g == L/kg). Uses an explicit value if set, else derives
        it from liquid volume / solid mass when both are known."""
        if self.liquid_solid_ratio is not None:
            return self.liquid_solid_ratio
        v, m = self.leachant.liquid_volume_mL, self.material.solid_mass_g
        if v is not None and m not in (None, 0):
            return round(v / m, 4)
        return None

    # -- serialization ----------------------------------------------------- #
    def to_dict(self) -> dict:
        """Nested, JSON-safe dict."""
        return {
            "material": asdict(self.material),
            "leachant": asdict(self.leachant),
            "process": asdict(self.process),
            "outputs": asdict(self.outputs),
            "liquid_solid_ratio": self.liquid_solid_ratio,
            "confidence": self.confidence,
            "notes": self.notes,
            "warnings": list(self.warnings),
        }

    def to_flat_dict(self) -> dict:
        """Flat field → value view (what the UI editor and the matrix consume)."""
        return {
            "material_name": self.material.material_name,
            "material_type": self.material.material_type,
            "solid_mass_g": self.material.solid_mass_g,
            "liquid_volume_mL": self.leachant.liquid_volume_mL,
            "liquid_solid_ratio": self.computed_ls_ratio(),
            "leachant_type": self.leachant.leachant_type,
            "leachant_concentration_M": self.leachant.leachant_concentration_M,
            "pH_initial": self.leachant.pH_initial,
            "time_min": self.process.time_min,
            "temperature_C": self.process.temperature_C,
            "CO2_condition": self.process.CO2_condition,
            "cover_condition": self.process.cover_condition,
            "centrifuge_used": self.process.centrifuge_used,
            "filtration_used": self.process.filtration_used,
            "filter_size_um": self.process.filter_size_um,
            "target_elements": list(self.outputs.target_elements),
            "desired_outputs": list(self.outputs.desired_outputs),
            "notes": self.outputs.notes or self.notes,
            "confidence": self.confidence,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_flat_dict(cls, d: dict) -> "SimulationScenario":
        """Rebuild a scenario from a (possibly user-edited) flat dict, coercing types."""
        d = dict(d or {})
        sc = cls(
            material=MaterialInput(
                material_name=as_str(d.get("material_name")),
                material_type=as_str(d.get("material_type")),
                solid_mass_g=as_float(d.get("solid_mass_g"))),
            leachant=LeachantInput(
                leachant_type=as_str(d.get("leachant_type")),
                leachant_concentration_M=as_float(d.get("leachant_concentration_M")),
                liquid_volume_mL=as_float(d.get("liquid_volume_mL")),
                pH_initial=as_float(d.get("pH_initial"))),
            process=ExperimentProcess(
                time_min=as_float(d.get("time_min")),
                temperature_C=as_float(d.get("temperature_C")),
                CO2_condition=as_str(d.get("CO2_condition")),
                cover_condition=as_str(d.get("cover_condition")),
                centrifuge_used=as_bool(d.get("centrifuge_used")),
                filtration_used=as_bool(d.get("filtration_used")),
                filter_size_um=as_float(d.get("filter_size_um"))),
            outputs=TargetOutputs(
                target_elements=as_str_list(d.get("target_elements")),
                desired_outputs=as_str_list(d.get("desired_outputs")),
                notes=as_str(d.get("notes"))),
            liquid_solid_ratio=as_float(d.get("liquid_solid_ratio")),
            confidence=as_float(d.get("confidence")) or 0.0,
            notes=as_str(d.get("notes")),
            warnings=as_str_list(d.get("warnings")),
        )
        return sc


# --------------------------------------------------------------------------- #
# The parse result (what a parser returns to the UI)
# --------------------------------------------------------------------------- #
@dataclass
class ScenarioParseResult:
    scenario: SimulationScenario
    source: str                              # SOURCE_AI / SOURCE_RULE / SOURCE_RULE_FALLBACK
    ok: bool = True
    error: str | None = None
    missing: list = field(default_factory=list)        # list[MissingInput]
    assumptions: list = field(default_factory=list)    # list[Assumption]
    warnings: list = field(default_factory=list)       # list[str]
    confidence: float = 0.0
    # The raw AI text, kept **in memory only** for an optional debug view — never written
    # to a run file and never treated as data.
    raw_response: str | None = None

    @property
    def used_ai(self) -> bool:
        return self.source == SOURCE_AI

    def source_label(self) -> str:
        return {
            SOURCE_AI: "AI extraction",
            SOURCE_RULE: "rule-based parsing",
            SOURCE_RULE_FALLBACK: "rule-based fallback (AI unavailable)",
        }.get(self.source, self.source)

    def to_summary(self) -> dict:
        """A compact, key-free summary (safe for display / debug)."""
        return {
            "source": self.source,
            "ok": self.ok,
            "error": self.error,
            "confidence": self.confidence,
            "n_missing": len(self.missing),
            "n_assumptions": len(self.assumptions),
            "n_warnings": len(self.warnings),
        }
