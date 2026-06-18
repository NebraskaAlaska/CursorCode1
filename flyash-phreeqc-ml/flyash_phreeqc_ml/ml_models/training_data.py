"""Training-row schema + eligibility + demo data (no scikit-learn; pure data plumbing).

A :class:`TrainingRow` carries the material / plastic / mixing-curing inputs, the measured
outputs, **and provenance** (source type, source id, citation/DOI, extraction confidence, and a
``user_review_status``). Provenance is first-class because the eligibility rule is the whole point:

* **Only ``approved`` rows are eligible for real training by default.** AI-extracted literature
  rows arrive as ``pending`` and must be reviewed before they can train a real model.
* **Rows without provenance, or low-confidence literature rows, are excluded by default** (the
  caller can opt them in for exploratory/demo training, but never silently).
* **Synthetic demo rows are quarantined** from real datasets unless explicitly included, and a
  model trained on them is labelled a demo (see :mod:`train` / :mod:`model_schema`).

This module never fabricates a real value: :func:`demo_rows` is clearly-labelled *synthetic*
data for workflow testing only, and :func:`from_composite_evidence` copies only what a paper
stated (missing → ``None``).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import model_schema

# --------------------------------------------------------------------------- #
# Provenance vocabulary
# --------------------------------------------------------------------------- #
SOURCE_LITERATURE = "literature"
SOURCE_LAB = "lab"
SOURCE_MANUAL = "manual"
SOURCE_DEMO = "synthetic_demo"
SOURCE_TYPES = (SOURCE_LITERATURE, SOURCE_LAB, SOURCE_MANUAL, SOURCE_DEMO)

REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_STATUSES = (REVIEW_PENDING, REVIEW_APPROVED, REVIEW_REJECTED)

#: Literature rows below this extraction confidence are excluded from real training by default.
DEFAULT_MIN_CONFIDENCE = 0.45
DEMO_N = 28


# --------------------------------------------------------------------------- #
# Training row
# --------------------------------------------------------------------------- #
@dataclass
class TrainingRow:
    """One training observation: inputs + outputs + provenance. All values optional / nullable."""

    # --- material / binder --------------------------------------------------
    fly_ash_class: str | None = None
    fly_ash_source: str | None = None
    SiO2_wt: float | None = None
    Al2O3_wt: float | None = None
    CaO_wt: float | None = None
    Fe2O3_wt: float | None = None
    MgO_wt: float | None = None
    Na2O_wt: float | None = None
    K2O_wt: float | None = None
    SO3_wt: float | None = None
    red_mud_percent: float | None = None
    cement_percent: float | None = None
    aggregate_percent: float | None = None
    # --- plastic ------------------------------------------------------------
    plastic_type: str | None = None
    plastic_form: str | None = None
    plastic_particle_size_mm: float | None = None
    plastic_dosage_percent: float | None = None
    plastic_replacement_basis: str | None = None
    # --- mixing / curing ----------------------------------------------------
    water_binder_ratio: float | None = None
    activator_type: str | None = None
    activator_concentration_M: float | None = None
    curing_time_days: float | None = None
    curing_temperature_C: float | None = None
    curing_condition: str | None = None
    specimen_geometry: str | None = None
    test_standard: str | None = None
    # --- outputs ------------------------------------------------------------
    compressive_strength_MPa: float | None = None
    flexural_strength_MPa: float | None = None
    density_g_cm3: float | None = None
    water_absorption_percent: float | None = None
    # --- provenance ---------------------------------------------------------
    source_type: str = SOURCE_MANUAL
    source_id: str | None = None
    citation: str | None = None
    doi: str | None = None
    extraction_confidence: float = 0.0
    user_review_status: str = REVIEW_PENDING
    notes: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TrainingRow":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})

    @property
    def has_provenance(self) -> bool:
        """A row has provenance if it carries a source type and a traceable id/citation/DOI.

        Lab / manual / demo rows are provenant by their source type; a literature row needs a
        DOI / citation / source id (a value with no traceable source is not evidence)."""
        if self.source_type in (SOURCE_LAB, SOURCE_MANUAL, SOURCE_DEMO):
            return True
        return bool(self.doi or self.citation or self.source_id)


def target_value(row: TrainingRow, target: str):
    """The row's value for ``target`` (or ``None``)."""
    return getattr(row, target, None)


# --------------------------------------------------------------------------- #
# Mapping a literature **composite** evidence row → a training row
# --------------------------------------------------------------------------- #
def _num(value):
    """Best-effort first number in a value (``"28 days"`` → 28.0); ``None`` if none."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def from_composite_evidence(ev: dict) -> TrainingRow:
    """Convert one Evidence-Library *composite* row (a dict from ``evidence_store.read_evidence``)
    into a training row.

    Carries provenance (DOI / citation / source) and the **extraction confidence**, and sets
    ``user_review_status = pending`` and ``source_type = literature`` — an AI-extracted row is
    **not** training-eligible until a human approves it. Only fields the paper stated are copied;
    everything else stays ``None`` (no fabrication). Density is converted kg/m³ → g/cm³.
    """
    ev = dict(ev or {})
    prov = ev.get("provenance") or {}
    density_kg = _num(ev.get("density_kg_m3"))
    return TrainingRow(
        fly_ash_source=ev.get("material_binder"),
        plastic_type=ev.get("plastic_type"),
        plastic_form=ev.get("plastic_form"),
        plastic_particle_size_mm=_num(ev.get("plastic_particle_size")),
        plastic_dosage_percent=_num(ev.get("plastic_dosage")),
        water_binder_ratio=_num(ev.get("water_binder_ratio")),
        curing_time_days=_num(ev.get("curing_time")),
        specimen_geometry=ev.get("specimen_geometry"),
        test_standard=None,
        compressive_strength_MPa=_num(ev.get("compressive_strength_MPa")),
        flexural_strength_MPa=_num(ev.get("flexural_strength_MPa")),
        density_g_cm3=(density_kg / 1000.0 if density_kg is not None else None),
        water_absorption_percent=_num(ev.get("water_absorption_pct")),
        source_type=SOURCE_LITERATURE,
        source_id=(prov.get("doi") or prov.get("url") or prov.get("title")),
        citation=ev.get("citation") or prov.get("citation"),
        doi=prov.get("doi"),
        extraction_confidence=float(ev.get("extraction_confidence") or 0.0),
        user_review_status=REVIEW_PENDING,
        notes=ev.get("notes"),
    )


def rows_from_evidence(evidence_rows) -> list:
    """Map a list of composite evidence dicts to training rows (pending review)."""
    return [from_composite_evidence(r) for r in (evidence_rows or [])]


# --------------------------------------------------------------------------- #
# Eligibility — what may train a *real* model
# --------------------------------------------------------------------------- #
def eligible_rows(rows, *, target=model_schema.DEFAULT_TARGET, allow_unapproved: bool = False,
                  min_confidence: float = DEFAULT_MIN_CONFIDENCE, require_provenance: bool = True,
                  include_demo: bool = False):
    """Filter ``rows`` to those eligible to train a real model for ``target``.

    Returns ``(kept, excluded)`` where ``excluded`` is a list of ``{source, citation, reason}``
    so the UI can show *why* a row was dropped. Defaults (the safe path):

    * the row must have a numeric value for ``target`` (else "no target value"),
    * ``user_review_status == approved`` (unless ``allow_unapproved``),
    * provenance present (unless ``require_provenance=False``),
    * for **literature** rows, ``extraction_confidence >= min_confidence``,
    * **synthetic_demo** rows are excluded unless ``include_demo=True``.
    """
    kept, excluded = [], []
    for row in rows or []:
        ident = {"source": row.source_type, "citation": row.citation or row.source_id or "—"}
        if target_value(row, target) is None:
            excluded.append({**ident, "reason": f"no {model_schema.target_label(target)} value"})
            continue
        if row.source_type == SOURCE_DEMO and not include_demo:
            excluded.append({**ident, "reason": "synthetic demo row (not included in real training)"})
            continue
        if not allow_unapproved and row.user_review_status != REVIEW_APPROVED:
            excluded.append({**ident, "reason": f"not approved (status: {row.user_review_status})"})
            continue
        if require_provenance and not row.has_provenance:
            excluded.append({**ident, "reason": "missing provenance (no DOI / citation / source)"})
            continue
        if row.source_type == SOURCE_LITERATURE and float(row.extraction_confidence or 0.0) < min_confidence:
            excluded.append({**ident,
                             "reason": f"low extraction confidence (< {min_confidence:g})"})
            continue
        kept.append(row)
    return kept, excluded


def summarize_eligibility(rows, *, target=model_schema.DEFAULT_TARGET, **kwargs) -> dict:
    """Counts for the UI: totals, approved, with-target, and the eligible count under the rules."""
    rows = list(rows or [])
    kept, excluded = eligible_rows(rows, target=target, **kwargs)
    return {
        "n_total": len(rows),
        "n_with_target": sum(1 for r in rows if target_value(r, target) is not None),
        "n_approved": sum(1 for r in rows if r.user_review_status == REVIEW_APPROVED),
        "n_pending": sum(1 for r in rows if r.user_review_status == REVIEW_PENDING),
        "n_demo": sum(1 for r in rows if r.source_type == SOURCE_DEMO),
        "n_eligible": len(kept),
        "n_excluded": len(excluded),
    }


def infer_dataset_source_type(rows) -> str:
    """The dataset's source type: the common one, ``"mixed"``, or ``"unknown"``."""
    kinds = {r.source_type for r in (rows or [])}
    if not kinds:
        return "unknown"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


# --------------------------------------------------------------------------- #
# Persistence (JSONL; safe path enforced by the caller via the registry dir)
# --------------------------------------------------------------------------- #
def load_dataset(path) -> list:
    """Read a training dataset JSONL (missing file → ``[]``; malformed lines skipped)."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(TrainingRow.from_dict(obj))
    return out


def save_dataset(path, rows) -> Path:
    """Write training rows to a JSONL file (parent created)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
    return p


# --------------------------------------------------------------------------- #
# Demo data — SYNTHETIC, clearly labelled, workflow-testing only
# --------------------------------------------------------------------------- #
def demo_rows(n: int = DEMO_N, seed: int = 0) -> list:
    """Generate tiny **synthetic** composite rows for UI/workflow testing.

    These are NOT real measurements and encode an arbitrary (made-up) relationship purely so the
    training/prediction workflow can be exercised. Every row is tagged ``source_type=synthetic_demo``
    and ``user_review_status=approved`` (self-approved demo data); a model trained on them is
    labelled a demo and must never be presented as validated or mixed with real evidence.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    plastic_types = ["PET", "HDPE", "PP"]
    forms = ["flake", "fiber", "pellet"]
    rows: list = []
    for i in range(int(n)):
        dosage = float(rng.uniform(0, 30))
        wb = float(rng.uniform(0.30, 0.60))
        curing = float(rng.choice([7, 14, 28, 56]))
        cao = float(rng.uniform(15, 30))
        sio2 = float(rng.uniform(30, 45))
        ptype = str(rng.choice(plastic_types))
        # Arbitrary synthetic relationship (NOT science): strength falls with dosage & w/b,
        # rises with curing & CaO, plus a small plastic-type effect and noise.
        strength = (40.0 - 0.6 * dosage - 25.0 * (wb - 0.40) + 0.15 * curing + 0.30 * cao
                    + (3.0 if ptype == "PET" else 0.0) + float(rng.normal(0, 2.0)))
        strength = max(1.0, strength)
        rows.append(TrainingRow(
            fly_ash_class="C", fly_ash_source="synthetic demo source",
            CaO_wt=round(cao, 1), SiO2_wt=round(sio2, 1),
            Al2O3_wt=round(float(rng.uniform(15, 25)), 1),
            plastic_type=ptype, plastic_form=str(rng.choice(forms)),
            plastic_dosage_percent=round(dosage, 1), plastic_replacement_basis="binder",
            water_binder_ratio=round(wb, 3), curing_time_days=curing,
            curing_temperature_C=23.0, curing_condition="sealed",
            specimen_geometry="50mm cube", test_standard="demo",
            compressive_strength_MPa=round(strength, 2),
            flexural_strength_MPa=round(0.15 * strength + float(rng.normal(0, 0.5)), 2),
            density_g_cm3=round(max(0.5, 1.85 - 0.012 * dosage + float(rng.normal(0, 0.03))), 3),
            water_absorption_percent=round(max(0.0, 4.0 + 0.25 * dosage + float(rng.normal(0, 0.5))), 2),
            source_type=SOURCE_DEMO, source_id=f"demo-{i}",
            citation="synthetic demo data (not a real measurement)",
            extraction_confidence=1.0, user_review_status=REVIEW_APPROVED,
            notes="synthetic demo — workflow testing only"))
    return rows
