"""Conservative **candidate-phase templates** for PHREEQC input previews.

When material elements are released into the leachate (see :mod:`source_terms`), PHREEQC can
predict which secondary phases precipitate — *but only if those phases are declared* (as
``EQUILIBRIUM_PHASES``) *and exist in the configured database*. This module offers small,
**transparent, non-exhaustive** starting phase sets for the common material families.

Principles:

* **Small and reviewed.** These are *starting* lists, not complete phase assemblages. Each
  phase says *why* it is here and whether it typically needs a specific database family.
* **Nothing is added silently.** The builder checks every phase against the configured database
  (:mod:`database_compatibility`) and adds only the ones it actually defines, listing the rest
  as warnings.
* **The default is aqueous-only** (no phases) — the most conservative choice.

Phase names use the spellings the common PHREEQC databases use (``Calcite``, ``Gibbsite``,
``Portlandite``, ``Gypsum``, ``Ettringite``, …). Many cement-specific phases (Portlandite,
Ettringite, C-S-H) exist only in a cementitious database (CEMDATA18) and are flagged as such.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import database_compatibility as _dbc

# Phase categories (for grouping / display only).
CAT_CALCIUM = "calcium"
CAT_ALUMINOSILICATE = "aluminosilicate / cementitious"
CAT_CARBONATE = "carbonate"
CAT_IRON = "iron"
CAT_SULFATE = "sulfate"


@dataclass(frozen=True)
class CandidatePhase:
    """One candidate precipitate phase — name + why + database expectations."""

    name: str
    why: str
    category: str = ""
    optional: bool = True
    required_family: str | None = None       # e.g. "cemdata" if it usually needs CEMDATA18
    absent_warning: str | None = None         # extra note if the database lacks it


@dataclass(frozen=True)
class PhaseTemplate:
    """A small, named, reviewed set of candidate phases (or none = aqueous-only)."""

    key: str
    label: str
    phases: tuple = ()                        # tuple[CandidatePhase]
    note: str = ""

    @property
    def is_aqueous_only(self) -> bool:
        return not self.phases

    def phase_names(self) -> list:
        return [p.name for p in self.phases]


# --------------------------------------------------------------------------- #
# The templates (deliberately small; NOT complete phase assemblages)
# --------------------------------------------------------------------------- #
AQUEOUS_ONLY = PhaseTemplate(
    key="aqueous_only",
    label="Aqueous only (no equilibrium phases) — default",
    phases=(),
    note="No precipitation is modelled. Only aqueous speciation and the saturation indices "
         "PHREEQC computes from the dissolved elements are reported. The conservative default.")

FLY_ASH_CEMENTITIOUS = PhaseTemplate(
    key="fly_ash_cementitious",
    label="Class C fly ash / high-pH cementitious (draft)",
    phases=(
        CandidatePhase("Portlandite", "Ca(OH)2 — buffers Ca at high pH", CAT_CALCIUM,
                       required_family=_dbc.FAMILY_CEMDATA,
                       absent_warning="Portlandite is absent from the general phreeqc.dat; it "
                                      "needs a cementitious database (CEMDATA18)."),
        CandidatePhase("Calcite", "CaCO3 — carbonation product", CAT_CARBONATE),
        CandidatePhase("Gibbsite", "Al(OH)3 — Al solubility control", CAT_ALUMINOSILICATE),
        CandidatePhase("SiO2(a)", "amorphous silica — Si solubility control",
                       CAT_ALUMINOSILICATE,
                       absent_warning="SiO2(a) is absent from some databases (phreeqc.dat has "
                                      "Chalcedony/Quartz instead)."),
        CandidatePhase("Gypsum", "CaSO4:2H2O — sulfate phase", CAT_SULFATE),
        CandidatePhase("Ettringite", "C3A·3CaSO4·32H2O — key cement sulfate phase", CAT_SULFATE,
                       required_family=_dbc.FAMILY_CEMDATA,
                       absent_warning="Ettringite is a CEMDATA18 phase; absent from phreeqc.dat."),
    ),
    note="A small, NON-EXHAUSTIVE starting set for high-pH cementitious systems. The cement "
         "phases (Portlandite, Ettringite, C-S-H, …) need CEMDATA18; with phreeqc.dat only the "
         "general hydroxide / carbonate / sulfate phases are available, so Ca/Al/Si solubility "
         "control at high pH is weak.")

RED_MUD = PhaseTemplate(
    key="red_mud",
    label="Red mud / bauxite residue (draft)",
    phases=(
        CandidatePhase("Hematite", "Fe2O3 — dominant Fe oxide", CAT_IRON),
        CandidatePhase("Goethite", "FeOOH — Fe oxyhydroxide", CAT_IRON),
        CandidatePhase("Gibbsite", "Al(OH)3 — Al control", CAT_ALUMINOSILICATE),
        CandidatePhase("Boehmite", "AlOOH — Al oxyhydroxide", CAT_ALUMINOSILICATE,
                       absent_warning="Boehmite is absent from some general databases."),
        CandidatePhase("Calcite", "CaCO3 — carbonate", CAT_CARBONATE),
    ),
    note="A small, NON-EXHAUSTIVE starting set for bauxite-residue systems. Ti phases "
         "(anatase/rutile) and Na-aluminosilicates are deliberately omitted until reviewed.")

GENERIC = PhaseTemplate(
    key="generic",
    label="Generic material (aqueous only)",
    phases=(),
    note="No material-specific phases — aqueous only. Pick a material family or add a reviewed "
         "custom phase list to model precipitation.")

TEMPLATES = (AQUEOUS_ONLY, FLY_ASH_CEMENTITIOUS, RED_MUD, GENERIC)
_BY_KEY = {t.key: t for t in TEMPLATES}
DEFAULT_TEMPLATE = AQUEOUS_ONLY


def get_template(key: str) -> PhaseTemplate:
    """Look up a template by key (falls back to the conservative aqueous-only default)."""
    return _BY_KEY.get(str(key), DEFAULT_TEMPLATE)


def custom_template(phase_names, *, label: str = "Custom reviewed phases") -> PhaseTemplate:
    """Build a template from a user-reviewed list of phase names."""
    phases = tuple(CandidatePhase(str(n), "user-supplied (reviewed)") for n in phase_names if n)
    return PhaseTemplate(key="custom", label=label, phases=phases,
                         note="User-supplied phase list — reviewed by you; still checked against "
                              "the configured database before being added.")
