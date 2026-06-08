"""Central configuration for the Fly Ash Lab Data Pipeline.

Everything in this module is an *editable assumption* or a schema definition — not a
fixed scientific fact. The Streamlit app surfaces the CO2/cost factors and scoring
weights as adjustable inputs and passes overrides into the calculation/scoring
functions, so the values here are only defaults / starting points.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Column schema
# ---------------------------------------------------------------------------
# Identity columns. ``mix_id`` is the formulation, ``specimen_id`` the individual
# cube/cylinder, ``test_id`` an optional unique test-result id, ``sample_id`` is kept
# for backwards compatibility / convenience.
ID_COLUMNS = ["sample_id", "mix_id", "specimen_id", "test_id"]

DATE_COLUMNS = ["date_cast", "date_tested"]

# Mass columns (grams). Used for binder maths and validation (no negatives).
MASS_COLUMNS = [
    "fly_ash_mass_g",
    "cement_mass_g",
    "water_mass_g",
    "red_mud_mass_g",
    "sand_mass_g",
]

# Optional specimen geometry / load columns used to back-calculate strength.
GEOMETRY_COLUMNS = [
    "specimen_shape",   # "cube" | "cylinder" | "prism" (free text accepted)
    "length_mm",
    "width_mm",
    "diameter_mm",
    "loaded_area_mm2",
    "peak_load_kN",
]

# Other numeric measurement columns.
MEASUREMENT_COLUMNS = [
    "curing_age_days",
    "flow_mm",
    "setting_time_min",
    "compressive_strength_MPa",
    "leachate_pH",
    "leachate_conductivity_uS_cm",
]

# All numeric columns (used for type coercion).
NUMERIC_COLUMNS = (
    MASS_COLUMNS
    + ["length_mm", "width_mm", "diameter_mm", "loaded_area_mm2", "peak_load_kN"]
    + MEASUREMENT_COLUMNS
)

# Free-text / categorical columns.
TEXT_COLUMNS = ["mix_type", "additive_type", "specimen_shape", "visual_notes",
                "photo_path", "data_status"]

# Allowed values for the data-status workflow column.
DATA_STATUS_VALUES = ["pending", "tested", "failed", "needs_retest"]

# The full expected column order for templates and exports.
EXPECTED_COLUMNS = [
    "sample_id",
    "mix_id",
    "specimen_id",
    "test_id",
    "date_cast",
    "date_tested",
    "curing_age_days",
    "mix_type",
    "fly_ash_mass_g",
    "cement_mass_g",
    "water_mass_g",
    "red_mud_mass_g",
    "sand_mass_g",
    "additive_type",
    "specimen_shape",
    "length_mm",
    "width_mm",
    "diameter_mm",
    "loaded_area_mm2",
    "peak_load_kN",
    "flow_mm",
    "setting_time_min",
    "compressive_strength_MPa",
    "leachate_pH",
    "leachate_conductivity_uS_cm",
    "data_status",
    "visual_notes",
    "photo_path",
]

# ---------------------------------------------------------------------------
# Editable assumptions: CO2 and cost factors
# ---------------------------------------------------------------------------
# Order-of-magnitude literature defaults. Cement clinker is roughly ~0.9 kg CO2/kg;
# Class C fly ash is treated as a near-zero embodied-carbon by-product. These are
# deliberately tunable in the app — do not treat them as authoritative constants.
DEFAULT_FACTORS = {
    "cement_co2_per_kg": 0.90,      # kg CO2 per kg cement avoided
    "fly_ash_co2_per_kg": 0.02,     # kg CO2 per kg fly ash used
    "cement_cost_per_kg": 0.12,     # currency per kg cement
    "fly_ash_cost_per_kg": 0.03,    # currency per kg fly ash
    "currency": "USD",
}

# ---------------------------------------------------------------------------
# Validation thresholds (editable assumptions)
# ---------------------------------------------------------------------------
WB_RATIO_MIN = 0.20        # below this a water/binder ratio is implausibly dry
WB_RATIO_MAX = 1.00        # above this it is implausibly wet
CV_HIGH_PCT = 15.0         # coefficient of variation (%) flagged as "very high"
PH_MIN = 0.0
PH_MAX = 14.0

# Leaching-risk reference points (editable). Risk rises as pH departs from neutral
# and as conductivity climbs. Conductivity values are in uS/cm.
PH_NEUTRAL = 7.0
PH_RISK_SPAN = 5.5         # |pH - 7| at/above which the pH sub-risk saturates to 1.0
CONDUCTIVITY_LOW = 500.0   # at/below this conductivity sub-risk is ~0
CONDUCTIVITY_HIGH = 5000.0 # at/above this conductivity sub-risk saturates to 1.0

# ---------------------------------------------------------------------------
# Reuse-ranking scoring presets (editable assumptions)
# ---------------------------------------------------------------------------
# Each application weights seven normalised sub-scores. Weights need not sum to 1;
# they are normalised at scoring time. Higher weight = more important for that use.
#
# Sub-scores (all normalised so that higher = more desirable for reuse):
#   strength       higher compressive strength is better
#   fly_ash_usage  higher fly-ash replacement is better (more reuse / lower carbon)
#   co2_saving     higher estimated CO2 saving is better
#   ph_safety      lower leaching pH risk is better (safety, not risk)
#   conductivity_safety  lower conductivity risk is better
#   flow           better workability is better
#   low_red_mud    lower red-mud demand is better (supply is limited)
SCORING_SUBSCORES = [
    "strength",
    "fly_ash_usage",
    "co2_saving",
    "ph_safety",
    "conductivity_safety",
    "flow",
    "low_red_mud",
]

SCORING_PRESETS = {
    "cement_replacement": {
        "strength": 0.30, "fly_ash_usage": 0.25, "co2_saving": 0.20,
        "ph_safety": 0.10, "conductivity_safety": 0.05, "flow": 0.05, "low_red_mud": 0.05,
    },
    "flowable_fill": {
        "strength": 0.05, "fly_ash_usage": 0.20, "co2_saving": 0.15,
        "ph_safety": 0.10, "conductivity_safety": 0.10, "flow": 0.35, "low_red_mud": 0.05,
    },
    "blocks_pavers": {
        "strength": 0.40, "fly_ash_usage": 0.15, "co2_saving": 0.15,
        "ph_safety": 0.10, "conductivity_safety": 0.05, "flow": 0.10, "low_red_mud": 0.05,
    },
    "road_base": {
        "strength": 0.25, "fly_ash_usage": 0.20, "co2_saving": 0.15,
        "ph_safety": 0.15, "conductivity_safety": 0.15, "flow": 0.05, "low_red_mud": 0.05,
    },
    "stabilized_disposal_monolith": {
        "strength": 0.15, "fly_ash_usage": 0.20, "co2_saving": 0.10,
        "ph_safety": 0.25, "conductivity_safety": 0.25, "flow": 0.00, "low_red_mud": 0.05,
    },
}

# Human-readable application labels for the UI / report.
APPLICATION_LABELS = {
    "cement_replacement": "Cement replacement",
    "flowable_fill": "Flowable fill",
    "blocks_pavers": "Blocks / pavers",
    "road_base": "Road base",
    "stabilized_disposal_monolith": "Stabilized disposal monolith",
}

PROJECT_TITLE = "Class C Fly Ash Reuse — Lab Data Pipeline Report"
