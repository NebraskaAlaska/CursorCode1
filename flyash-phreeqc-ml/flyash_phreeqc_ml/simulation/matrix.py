"""Turn a confirmed scenario into a **simulation plan/matrix** (a table of intended runs).

No PHREEQC is executed — every row's ``status`` is ``plan_only`` and the table is labelled
accordingly. The builder is designed so it can later fan a single scenario out over
parameter ranges (concentration / time / temperature / L:S) by passing ``ranges``; with no
ranges it returns exactly one row.
"""
from __future__ import annotations

import itertools

import pandas as pd

from .scenario_schema import SimulationScenario

MATRIX_COLUMNS = [
    "scenario_id", "material", "solid_mass_g", "liquid_volume_mL", "liquid_solid_ratio",
    "leachant_type", "leachant_concentration_M", "time_min", "temperature_C",
    "CO2_condition", "target_elements", "desired_outputs", "status",
]

STATUS_PLAN_ONLY = "plan_only"

# Fields a future UI may sweep as a range/list. Order fixes the scenario_id numbering.
RANGEABLE_FIELDS = ("leachant_concentration_M", "time_min", "temperature_C", "liquid_solid_ratio")


def _join(values) -> str:
    return ", ".join(str(v) for v in (values or []))


def build_simulation_matrix(scenario: SimulationScenario, *, ranges: dict | None = None,
                            status: str = STATUS_PLAN_ONLY) -> pd.DataFrame:
    """Build the plan matrix for ``scenario``.

    ``ranges`` (optional) maps any of :data:`RANGEABLE_FIELDS` to a list of values; the
    matrix is the Cartesian product of the provided ranges (one row per combination). With
    no ranges, a single-row plan is returned. Every row is ``status=plan_only`` — **no
    PHREEQC is run here.**
    """
    flat = scenario.to_flat_dict()
    ranges = {k: list(v) for k, v in (ranges or {}).items() if k in RANGEABLE_FIELDS and v}
    swept = [k for k in RANGEABLE_FIELDS if k in ranges]
    combos = list(itertools.product(*[ranges[k] for k in swept])) if swept else [()]

    material = flat.get("material_name") or flat.get("material_type") or "unspecified"
    elements = _join(flat.get("target_elements"))
    outputs = _join(flat.get("desired_outputs"))

    rows = []
    for i, combo in enumerate(combos, start=1):
        overrides = dict(zip(swept, combo))

        def pick(key):
            return overrides.get(key, flat.get(key))

        rows.append({
            "scenario_id": f"SIM-{i:03d}",
            "material": material,
            "solid_mass_g": flat.get("solid_mass_g"),
            "liquid_volume_mL": flat.get("liquid_volume_mL"),
            "liquid_solid_ratio": pick("liquid_solid_ratio"),
            "leachant_type": flat.get("leachant_type"),
            "leachant_concentration_M": pick("leachant_concentration_M"),
            "time_min": pick("time_min"),
            "temperature_C": pick("temperature_C"),
            "CO2_condition": flat.get("CO2_condition"),
            "target_elements": elements,
            "desired_outputs": outputs,
            "status": status,
        })
    return pd.DataFrame(rows, columns=MATRIX_COLUMNS)
