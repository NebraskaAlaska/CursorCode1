"""Dataset + model **profiles** — the first generalization layer (additive only).

The pipeline was written for *Class C fly ash + PHREEQC*. To begin generalizing
beyond that pairing **without renaming the package or breaking the working pipeline**,
this module introduces two small, immutable description objects:

* :class:`DatasetProfile` — what a measured dataset looks like: its id/time/replicate
  columns, variable columns + units, the condition column and its code dictionary
  (e.g. the OA/PF/GS cup covers), the fields + numeric tolerances that matter for
  mapping, and how to parse a sample id.
* :class:`ModelProfile` — what the prediction model is: its display *name* (so UI
  strings like "needs new {model} simulation" are profile-driven), its prediction
  metadata fields, and its parser entry point.

The existing ``config.py`` constants remain the **single source of truth**; the
fly-ash / PHREEQC profiles here only *reference* them. Profiles are threaded through
the existing seams with a fly-ash default, so all current behaviour is unchanged —
passing a different profile is what makes the same code work for another dataset.
Nothing here does chemistry or ML.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config, units


@dataclass(frozen=True)
class DatasetProfile:
    """How one measured dataset is shaped (columns, codes, mapping fields, tolerances)."""

    name: str
    id_column: str = "sample_id"
    time_column: str | None = "time_min"
    replicate_column: str = "replicate_id"
    # Regex that pulls a replicate number out of a sample id (R1 / rep2 / batch3 …).
    replicate_pattern: str = r"(?:^|[-_ ])(?:R|REP|REPLICATE|BATCH)\s*[-_]?\s*(\d+)\b"
    # --- Batch semantics (additive; see replicates.REPLICATE_ROLE_DEFINITIONS) ---
    # A distinct *preparation/batch* of the same condition (not a time point, not a
    # repeat measurement). The batch id is read from ``batch_column`` if present, else
    # parsed from the sample id with ``batch_pattern`` (group 1). When ``group_by_batch``
    # is True the batch becomes part of ``condition_key`` (batches compared separately);
    # otherwise batches fold into the condition like true replicates.
    batch_column: str | None = None
    batch_pattern: str | None = None
    group_by_batch: bool = False
    # Optional regex for parsing the whole sample id (documentation / future use).
    sample_id_pattern: str | None = None
    # The column that carries the experimental condition code (cup cover for fly ash).
    condition_column: str = "CO2_condition"
    # code -> {"description": str, "caution": str}. The UI reads this dict.
    condition_codes: dict = field(default_factory=dict)
    # Measured variable columns and their units (display only).
    variable_columns: tuple = ()
    variable_units: dict = field(default_factory=dict)
    # Variables offered in the measured-data overview (subset that makes sense to plot).
    overview_variables: tuple = ()
    # Fields used to group replicates into a condition and to judge a mapping.
    important_fields: tuple = ()
    # Per-field numeric tolerance for "matches" decisions.
    tolerances: dict = field(default_factory=dict)
    # variable -> (measured_column, model_prediction_column) for the comparison.
    comparison_variable_spec: dict = field(default_factory=dict)
    # "fly_ash" keeps the bespoke leachant/CO2 condition_key; anything else uses the
    # generic important-fields key builder.
    grouping: str = "generic"
    # --- Residual-correction model feature encoding (additive; used by ml.residual_model) ---
    # Raw numeric metadata columns used as continuous features (missing values imputed).
    feature_numeric_fields: tuple = ()
    # Categorical feature names. Names ``leachant_family`` / ``condition_code`` are
    # *derived* (via scenarios); any other name is read straight from that column.
    feature_categorical_fields: tuple = ()
    # --- Import unit contract (additive; used by import_mapping) ---
    # Per convertible column, the source units the importer accepts. An undeclared unit
    # is refused (no guess). Empty -> the importer falls back to the units.py default set.
    accepted_units: dict = field(default_factory=dict)
    # --- Batch-reaction mass balance (additive; used by mass_balance) ---
    # Empty `mass_balance_elements` = the feature is OFF for this dataset (the default,
    # so FLY_ASH_PROFILE is unchanged). Declaring elements opts a dataset in; the column
    # names default to the config batch schema and the units are declared, not guessed.
    mass_balance_elements: tuple = ()                 # e.g. ("Ca", "Si", "Al", "Fe")
    material_mass_column: str = "material_mass_g"
    solid_mass_column: str = "solid_mass_g"           # optional in data; flagged if absent
    liquid_volume_column: str = "liquid_volume_mL"
    starting_content_unit: str = "wt%"                # unit of {el}_starting_content
    solid_residue_unit: str = "wt%"                   # unit of {el}_solid_residue
    liquid_conc_unit: str = "mM"                      # unit of the measured liquid {el}_mM
    # --- Gap attribution via PHREEQC (Prompt 24; additive) ---
    # Candidate precipitate phases (PHREEQC phase name -> element) added to
    # EQUILIBRIUM_PHASES and read back from the selected output.
    mass_balance_candidate_phases: dict = field(default_factory=dict)
    # CONFIRMED for the fly-ash filtration protocol = False: a PHREEQC-predicted
    # precipitate leaves with the filtrate and is NOT in the measured solid residue, so
    # it counts toward EXPLAINING the gap (attribution_to_gap = min(P, gap)). Set True
    # for a protocol where precipitates are retained in the assayed solid (then they are
    # already in n_solid and do not reduce the gap). See docs/mass_balance.md.
    precipitate_in_measured_solid: bool = False
    # --- Material side (Prompt 28; additive) ---------------------------------- #
    # The material whose batch chemistry this dataset measures. When set, the
    # material's elements / candidate phases / precipitate flag / reagents are the
    # source of truth (read via the module-level resolvers below); when None the
    # DatasetProfile's own batch fields above are used (legacy / fly-ash-as-built).
    material: "MaterialProfile | None" = None


@dataclass(frozen=True)
class ModelProfile:
    """How the prediction model is described (name + metadata + parser entry point).

    ``parser_entry_point`` is the dotted module path of the parser that turns this
    model's raw output into a frame :func:`scenarios.build_scenario_manifest` accepts.
    Carrying it on the profile is what lets the app stay model-agnostic: the manifest
    (and everything downstream) does not care which parser produced the predictions.
    """

    name: str
    # Metadata columns carried from the model side (shown in advanced views).
    prediction_metadata_fields: tuple = ()
    # Dotted path to the parser that turns raw model output into a tidy frame.
    parser_entry_point: str = ""
    # How the model's output reaches the app: a parsed file format (PHREEQC .pqo) or a
    # generic prediction CSV. The Data tab uses this to choose the import path.
    source_kind: str = "phreeqc"

    def load_parser(self):
        """Import and return the parser module named by ``parser_entry_point``."""
        if not self.parser_entry_point:
            raise ValueError(f"model profile {self.name!r} has no parser_entry_point")
        import importlib
        return importlib.import_module(self.parser_entry_point)


# --------------------------------------------------------------------------- #
# Material profile (Prompt 28) — push material/reagent/phase specifics into a profile
# --------------------------------------------------------------------------- #
# Provenance vocabulary for a declared assay (mirrors ai.literature so the quarantine
# rule is the same): a literature-*proposed* assay is **never usable in a calculation**
# until a human confirms it (then it becomes literature-confirmed).
ASSAY_MEASURED = "measured"
ASSAY_LITERATURE_CONFIRMED = "literature-confirmed"
ASSAY_LITERATURE_PROPOSED = "literature-proposed"
# Only these provenances may feed a closure; literature-proposed stays quarantined.
USABLE_ASSAY_PROVENANCE = (ASSAY_MEASURED, ASSAY_LITERATURE_CONFIRMED)


@dataclass(frozen=True)
class AssayValue:
    """A declared bulk element assay for a material, carrying its provenance.

    ``provenance`` is one of :data:`ASSAY_MEASURED` / :data:`ASSAY_LITERATURE_CONFIRMED`
    / :data:`ASSAY_LITERATURE_PROPOSED`. A *proposed* value is quarantined: it is kept
    for display but :func:`is_usable` is False, so it can never enter a calculation until
    a human confirms it (Prompt 24/26 rule).
    """

    element: str
    value: float | None = None
    unit: str = "wt%"
    provenance: str = ASSAY_MEASURED
    citation: str | None = None      # DOI/link required when provenance is literature-*

    @property
    def is_usable(self) -> bool:
        return self.provenance in USABLE_ASSAY_PROVENANCE and self.value is not None


@dataclass(frozen=True)
class MaterialProfile:
    """The material side of a batch-reaction dataset (Prompt 28; additive).

    Bundles everything material/reagent/phase-specific that Prompts 22–25 + the
    incompleteness model need, so the same code runs for a *new* material by swapping
    this object — no fly-ash elements or phases are hard-coded anywhere downstream.
    """

    material_id: str
    display_name: str
    # All elements this material's chemistry cares about (display + docs).
    relevant_elements: tuple = ()
    # Subset used for the batch closure (opt-in; empty = mass balance OFF for it).
    mass_balance_elements: tuple = ()
    # Candidate precipitate phases for Prompt-24 attribution: PHREEQC phase -> element.
    candidate_phases: dict = field(default_factory=dict)
    # Prompt-23 filtration flag: does a predicted precipitate sit in the measured solid?
    precipitate_in_measured_solid: bool = False
    # Reagents typically used to leach/activate this material (e.g. ("NaOH",)).
    default_reagents: tuple = ()
    # Declared typical bulk assay per element, provenance-flagged (see AssayValue).
    declared_assay: dict = field(default_factory=dict)
    # Assay + liquid units and the batch column names (default to the config schema).
    starting_content_unit: str = "wt%"
    solid_residue_unit: str = "wt%"
    liquid_conc_unit: str = "mM"
    material_mass_column: str = "material_mass_g"
    solid_mass_column: str = "solid_mass_g"
    liquid_volume_column: str = "liquid_volume_mL"

    def usable_assay(self, element: str) -> AssayValue | None:
        """The declared assay for ``element`` **only if usable** (never a proposed one)."""
        av = self.declared_assay.get(element)
        return av if (av is not None and av.is_usable) else None


# --------------------------------------------------------------------------- #
# Resolvers — the active profile's material side (material first, legacy fallback)
# --------------------------------------------------------------------------- #
# Downstream modules (mass_balance, attribution, report recovery, incompleteness_model)
# call THESE, never a hard-coded element/phase list. When a profile carries a material
# the material is authoritative; otherwise the DatasetProfile's own batch fields are used
# (so profiles built the old way — incl. the test profiles — are unchanged).
def _material(profile):
    return getattr(profile, "material", None)


def mass_balance_elements(profile) -> tuple:
    m = _material(profile)
    if m is not None and getattr(m, "mass_balance_elements", ()):
        return tuple(m.mass_balance_elements)
    return tuple(getattr(profile, "mass_balance_elements", ()) or ())


def candidate_phases(profile) -> dict:
    m = _material(profile)
    if m is not None and getattr(m, "candidate_phases", None):
        return dict(m.candidate_phases)
    return dict(getattr(profile, "mass_balance_candidate_phases", {}) or {})


def precipitate_in_measured_solid(profile) -> bool:
    m = _material(profile)
    if m is not None:
        return bool(m.precipitate_in_measured_solid)
    return bool(getattr(profile, "precipitate_in_measured_solid", False))


def relevant_elements(profile) -> tuple:
    m = _material(profile)
    if m is not None and getattr(m, "relevant_elements", ()):
        return tuple(m.relevant_elements)
    return mass_balance_elements(profile)


def default_reagents(profile) -> tuple:
    m = _material(profile)
    return tuple(getattr(m, "default_reagents", ()) or ()) if m is not None else ()


def material_display_name(profile) -> str | None:
    m = _material(profile)
    return getattr(m, "display_name", None) if m is not None else None


def usable_declared_assay(profile, element: str) -> AssayValue | None:
    """A material's declared assay for ``element``, only when usable (not proposed)."""
    m = _material(profile)
    return m.usable_assay(element) if m is not None else None


def dataset_profile_from_material(material: MaterialProfile, *, name: str | None = None,
                                  **dataset_kwargs) -> DatasetProfile:
    """Build a :class:`DatasetProfile` whose batch fields are filled from ``material``.

    The material is the single source of truth: its elements / phases / precipitate flag
    / assay units / column names populate the corresponding DatasetProfile fields AND the
    material is attached (so the resolvers above also see it). Any other DatasetProfile
    kwargs (condition codes, important_fields, grouping, comparison spec, feature fields…)
    are passed through unchanged.
    """
    return DatasetProfile(
        name=name or material.display_name,
        material=material,
        mass_balance_elements=tuple(material.mass_balance_elements),
        mass_balance_candidate_phases=dict(material.candidate_phases),
        precipitate_in_measured_solid=bool(material.precipitate_in_measured_solid),
        material_mass_column=material.material_mass_column,
        solid_mass_column=material.solid_mass_column,
        liquid_volume_column=material.liquid_volume_column,
        starting_content_unit=material.starting_content_unit,
        solid_residue_unit=material.solid_residue_unit,
        liquid_conc_unit=material.liquid_conc_unit,
        **dataset_kwargs,
    )


# --------------------------------------------------------------------------- #
# Fly-ash dataset profile — populated from config.py (the single source of truth)
# --------------------------------------------------------------------------- #
# Experiment-side cup-cover codes (OA/PF/GS), referenced from the config dict.
_FLY_ASH_CONDITION_CODES = {
    code: config.CONDITION_CODE_DESCRIPTIONS[code]
    for code in ("OA", "PF", "GS")
}

# Measured variables shown in the overview: pH + the ICP element columns.
_FLY_ASH_OVERVIEW_VARIABLES = (
    "final_pH", "Ca_mM", "Si_mM", "Al_mM", "Fe_mM",
    "Na_mM", "K_mM", "Sc_ppb", "total_REE_ppb",
)
_FLY_ASH_VARIABLE_UNITS = {
    "final_pH": "pH",
    "Ca_mM": "mM", "Si_mM": "mM", "Al_mM": "mM", "Fe_mM": "mM",
    "Na_mM": "mM", "K_mM": "mM", "Sc_ppb": "ppb", "total_REE_ppb": "ppb",
}
# Comparison variable -> (measured_column, model_prediction_column). The element
# residuals come straight from config.RESIDUAL_ELEMENTS; pH is added explicitly.
_FLY_ASH_COMPARISON_SPEC = {
    "final_pH": ("final_pH", "phreeqc_pH"),
    **{f"{el}_mM": (f"{el}_mM", f"phreeqc_{el}_mM") for el in config.RESIDUAL_ELEMENTS},
}

# The fly-ash material side (Prompt 28). Closure stays OFF for fly ash (no
# ``mass_balance_elements``), so behaviour is unchanged — but the material now declares
# the elements its chemistry involves (Ca/Si/Al/Fe/Na/K) and its reagent (NaOH), so the
# same material abstraction that drives a new material also describes fly ash.
FLY_ASH_MATERIAL = MaterialProfile(
    material_id="class_c_fly_ash",
    display_name="Class C fly ash",
    relevant_elements=("Ca", "Si", "Al", "Fe", "Na", "K"),
    mass_balance_elements=(),                 # closure OFF for fly ash (unchanged)
    candidate_phases={},
    precipitate_in_measured_solid=False,
    default_reagents=("NaOH",),
    declared_assay={},                        # no committed typical assay shipped
)

FLY_ASH_PROFILE = DatasetProfile(
    name="Class C fly ash",
    id_column="sample_id",
    time_column="time_min",
    condition_column="CO2_condition",
    material=FLY_ASH_MATERIAL,
    condition_codes=_FLY_ASH_CONDITION_CODES,
    variable_columns=tuple(config.EXPERIMENTAL_NUMERIC_COLUMNS),
    variable_units=_FLY_ASH_VARIABLE_UNITS,
    overview_variables=_FLY_ASH_OVERVIEW_VARIABLES,
    # Fields the bespoke condition_key + mapping use (leachant/molarity/cover/time/L:S).
    important_fields=("leachant", "NaOH_M", "acid_M", "CO2_condition",
                      "time_min", "liquid_solid_ratio", "temperature_C"),
    tolerances={"liquid_solid_ratio": 1e-6, "temperature_C": 1.0},
    comparison_variable_spec=_FLY_ASH_COMPARISON_SPEC,
    grouping="fly_ash",
    # Residual-model features: molarity, L/S, time (numeric) + leachant family and the
    # OA/PF/GS cover code (one-hot). Time is imputed when an exact mapping omits it.
    feature_numeric_fields=("NaOH_M", "liquid_solid_ratio", "time_min"),
    feature_categorical_fields=("leachant_family", "condition_code"),
    # Each convertible ICP column accepts mg/L, ppm, ppb, or mM (the lab-import set).
    accepted_units={f"{el}_mM": units.LAB_CONCENTRATION_SOURCE_UNITS
                    for el in config.RESIDUAL_ELEMENTS + ["Na", "K"]},
)


# --------------------------------------------------------------------------- #
# PHREEQC model profile
# --------------------------------------------------------------------------- #
PHREEQC_PROFILE = ModelProfile(
    name="PHREEQC",
    prediction_metadata_fields=("source_file", "simulation", "state", "solution_number"),
    parser_entry_point="flyash_phreeqc_ml.parsers.pqo_parser",
    source_kind="phreeqc",
)


# A model-agnostic prediction source: any model can supply a CSV that meets the
# documented contract (see docs/model_prediction_format.md). The manifest builder
# consumes its parser's output exactly like PHREEQC's, proving generality.
GENERIC_CSV_PROFILE = ModelProfile(
    name="Generic model (CSV)",
    prediction_metadata_fields=("leachant", "NaOH_M", "time_min",
                                "liquid_solid_ratio", "CO2_condition", "temperature_C"),
    parser_entry_point="flyash_phreeqc_ml.parsers.generic_prediction_parser",
    source_kind="generic_csv",
)


def default_dataset_profile() -> DatasetProfile:
    """The dataset profile assumed when a caller passes none (keeps current behaviour)."""
    return FLY_ASH_PROFILE


def default_model_profile() -> ModelProfile:
    """The model profile assumed when a caller passes none."""
    return PHREEQC_PROFILE


# --------------------------------------------------------------------------- #
# Second material (stub) — bauxite residue / red mud — proves the abstraction
# --------------------------------------------------------------------------- #
# Illustrative, not a validated red-mud parameter set: different elements (Ti/V/Fe/Al,
# with REE among the relevant set), different candidate phases (anatase / rutile /
# hematite), a different reagent set, and the OPPOSITE filtration flag from fly ash —
# so a fly-ash assumption leaking anywhere would change the answer. Ti/V molar masses
# were added to units.MOLAR_MASSES. Declared assays carry provenance; the Ti value is
# literature-PROPOSED → quarantined (never usable until confirmed).
RED_MUD_MATERIAL = MaterialProfile(
    material_id="red_mud",
    display_name="Bauxite residue (red mud)",
    relevant_elements=("Fe", "Al", "Ti", "V", "Na", "Ca", "REE"),
    mass_balance_elements=("Ti", "V", "Fe", "Al"),
    candidate_phases={"Anatase": "Ti", "Rutile": "Ti", "Hematite": "Fe",
                      "Gibbsite": "Al", "Boehmite": "Al"},
    precipitate_in_measured_solid=True,        # opposite of fly ash (stub assumption)
    default_reagents=("NaOH", "H2SO4"),
    declared_assay={
        # measured → usable; literature-confirmed → usable (carries a DOI);
        # literature-proposed → quarantined (is_usable False) until a human confirms it.
        "Fe": AssayValue("Fe", value=30.0, unit="wt%", provenance=ASSAY_LITERATURE_CONFIRMED,
                         citation="https://doi.org/10.0000/redmud-fe"),
        "Ti": AssayValue("Ti", value=5.0, unit="wt%", provenance=ASSAY_LITERATURE_PROPOSED),
    },
)

RED_MUD_PROFILE = dataset_profile_from_material(
    RED_MUD_MATERIAL,
    grouping="generic",
    condition_column="reagent",
    important_fields=("reagent", "reagent_conc_M", "liquid_solid_ratio"),
    overview_variables=("Ti_mM", "V_mM", "Fe_mM", "Al_mM"),
    comparison_variable_spec={f"{el}_mM": (f"{el}_mM", f"phreeqc_{el}_mM")
                              for el in ("Ti", "V", "Fe", "Al")},
    feature_numeric_fields=("reagent_conc_M", "liquid_solid_ratio"),
    feature_categorical_fields=("reagent",),
)


# --------------------------------------------------------------------------- #
# Profile-creation path (Prompt 28) — a researcher defines a material in JSON/YAML
# --------------------------------------------------------------------------- #
# A material/experiment is defined in a small JSON (no deps) or YAML (if PyYAML is
# installed) file; see docs/defining_a_material.md. No code is needed to add a material.
def assay_value_from_dict(element: str, d: dict) -> AssayValue:
    """Build an :class:`AssayValue` from a plain dict (validates provenance + citation)."""
    prov = str(d.get("provenance", ASSAY_MEASURED))
    if prov not in (ASSAY_MEASURED, ASSAY_LITERATURE_CONFIRMED, ASSAY_LITERATURE_PROPOSED):
        raise ValueError(
            f"assay for {element!r}: provenance {prov!r} not recognized "
            f"(use {ASSAY_MEASURED!r}, {ASSAY_LITERATURE_CONFIRMED!r}, or "
            f"{ASSAY_LITERATURE_PROPOSED!r}).")
    citation = d.get("citation")
    if prov in (ASSAY_LITERATURE_CONFIRMED, ASSAY_LITERATURE_PROPOSED) and not citation:
        raise ValueError(f"assay for {element!r}: a literature provenance needs a "
                         "citation (DOI/URL).")
    value = d.get("value")
    return AssayValue(element=element, value=(None if value is None else float(value)),
                      unit=str(d.get("unit", "wt%")), provenance=prov,
                      citation=(str(citation) if citation else None))


def material_profile_from_dict(spec: dict) -> MaterialProfile:
    """Build a :class:`MaterialProfile` from a parsed JSON/YAML spec (no code needed).

    Required: ``material_id``, ``display_name``. Everything else is optional and additive.
    Declared assays are validated for provenance + citation; a ``literature-proposed``
    assay is kept **quarantined** (``is_usable`` False) — it can never enter a calculation
    until a human confirms it.
    """
    if not spec.get("material_id") or not spec.get("display_name"):
        raise ValueError("a material spec needs 'material_id' and 'display_name'.")
    assays_raw = spec.get("declared_assay") or {}
    declared = {el: assay_value_from_dict(el, d) for el, d in assays_raw.items()}
    return MaterialProfile(
        material_id=str(spec["material_id"]),
        display_name=str(spec["display_name"]),
        relevant_elements=tuple(spec.get("relevant_elements", ()) or ()),
        mass_balance_elements=tuple(spec.get("mass_balance_elements", ()) or ()),
        candidate_phases=dict(spec.get("candidate_phases", {}) or {}),
        precipitate_in_measured_solid=bool(spec.get("precipitate_in_measured_solid", False)),
        default_reagents=tuple(spec.get("default_reagents", ()) or ()),
        declared_assay=declared,
        starting_content_unit=str(spec.get("starting_content_unit", "wt%")),
        solid_residue_unit=str(spec.get("solid_residue_unit", "wt%")),
        liquid_conc_unit=str(spec.get("liquid_conc_unit", "mM")),
        material_mass_column=str(spec.get("material_mass_column", "material_mass_g")),
        solid_mass_column=str(spec.get("solid_mass_column", "solid_mass_g")),
        liquid_volume_column=str(spec.get("liquid_volume_column", "liquid_volume_mL")),
    )


def dataset_profile_from_spec(spec: dict) -> DatasetProfile:
    """Build a full :class:`DatasetProfile` (material + dataset shape) from one spec dict.

    ``spec["material"]`` defines the material (see :func:`material_profile_from_dict`);
    ``spec["dataset"]`` carries the dataset-shape kwargs (condition column/codes,
    important_fields, comparison_variable_spec, overview_variables, sample_id_pattern,
    grouping, feature fields…), passed through to :class:`DatasetProfile` unchanged.
    """
    material = material_profile_from_dict(spec.get("material") or {})
    dataset_kwargs = dict(spec.get("dataset") or {})
    # JSON gives lists/dicts; the dataclass wants tuples for the *_fields entries.
    for key in ("important_fields", "overview_variables", "variable_columns",
                "feature_numeric_fields", "feature_categorical_fields"):
        if key in dataset_kwargs and dataset_kwargs[key] is not None:
            dataset_kwargs[key] = tuple(dataset_kwargs[key])
    return dataset_profile_from_material(material, **dataset_kwargs)


def load_material_spec(path) -> dict:
    """Read a material spec file → a plain dict. JSON always; YAML if PyYAML is installed."""
    import json
    from pathlib import Path
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # optional; not a hard dependency
        except Exception as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "YAML material specs need PyYAML (`pip install pyyaml`), or use JSON "
                "(`.json`), which needs no extra dependency.") from exc
        return yaml.safe_load(text)
    return json.loads(text)


def load_dataset_profile(path) -> DatasetProfile:
    """Load a material/dataset spec file and build its :class:`DatasetProfile`."""
    return dataset_profile_from_spec(load_material_spec(path))
