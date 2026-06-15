"""The batch-reaction chemistry is material-agnostic via the profile (Prompt 28).

Pins the architectural claim behind matrix (g): mass_balance, attribution, the recovery
report section, and the incompleteness model read the elements / candidate phases /
precipitate flag from the **active profile's material** (the `profiles.*` resolvers), and
must NOT hard-code fly-ash elements or phase names. Verified by source inspection
(mirrors test_manifest_model_agnostic.py).
"""
from __future__ import annotations

import inspect

import pytest

from flyash_phreeqc_ml import attribution, mass_balance, report
from flyash_phreeqc_ml.ml import incompleteness_model

# Every module that does batch chemistry off the profile's material side.
MATERIAL_MODULES = [mass_balance, attribution, report, incompleteness_model]

# A hard-coded fly-ash element list, a config element constant, or any literal candidate
# phase name would be a leak — elements/phases must come from the profile, not the code.
FORBIDDEN_TOKENS = [
    "config.RESIDUAL_ELEMENTS",
    "Calcite", "Portlandite", "Gibbsite",          # fly-ash candidate phases
    "Anatase", "Hematite", "Rutile", "Boehmite",   # red-mud candidate phases
]
FORBIDDEN_ELEMENT_TUPLES = ['("Ca", "Si"', "('Ca', 'Si'", '("Ti", "V"', "('Ti', 'V'"]


@pytest.mark.parametrize("module", MATERIAL_MODULES, ids=lambda m: m.__name__)
def test_no_hardcoded_elements_or_phases(module):
    src = inspect.getsource(module)
    for tok in FORBIDDEN_TOKENS:
        assert tok not in src, f"{module.__name__} hard-codes {tok!r} (must read it from the profile)"
    for tup in FORBIDDEN_ELEMENT_TUPLES:
        assert tup not in src, f"{module.__name__} hard-codes an element tuple {tup!r}"


@pytest.mark.parametrize("module", MATERIAL_MODULES, ids=lambda m: m.__name__)
def test_reads_elements_from_profile_resolver(module):
    """Each module enumerates batch elements via the profile resolver, not a literal list."""
    src = inspect.getsource(module)
    assert "profiles.mass_balance_elements(" in src, \
        f"{module.__name__} must resolve elements via profiles.mass_balance_elements()"


def test_attribution_reads_phases_and_flag_from_profile():
    """Attribution resolves candidate phases + the PER-ELEMENT precipitate flag (Prompt 28)."""
    src = inspect.getsource(attribution)
    assert "profiles.candidate_phases(" in src
    assert "profiles.precipitate_in_measured_solid_for(" in src
