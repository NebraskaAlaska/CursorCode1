"""ML **surrogate prediction engine** for unsupported (non-PHREEQC) domains.

The first model family predicts **polymer-composite / mechanical properties** (compressive
strength first) from material / mix / curing inputs. It is a *prediction* engine, deliberately
separate from the geochemical result path:

* **No AI/LLM here.** This package contains no AI client and no LLM call — numbers come from a
  trained scikit-learn model, never from a language model. (The literature *evidence* it can be
  trained on was extracted upstream in :mod:`flyash_phreeqc_ml.literature`; review/approval gates
  what enters training.)
* **Off the geochemical result path.** It does not import PHREEQC execution, the simulation
  planner, or the measured-vs-model comparison/mapping modules, and they do not import it (pinned
  by ``tests/test_ai_boundary.py``).
* **Honest by construction.** Real training is **gated** on enough *approved* rows, metrics are
  out-of-sample, predictions carry an interval + applicability warnings, models are at most
  ``experimental`` (never ``validated``), and a clearly-labelled **demo** mode trains on synthetic
  data for workflow testing only.

scikit-learn is an *optional* dependency, imported lazily inside training/prediction; importing
this package (and the schema / registry helpers the UI and assistant use) works without it.
"""
from __future__ import annotations

from . import (  # noqa: F401
    feature_schema,
    model_card,
    model_registry,
    model_schema,
    predict,
    preprocessing,
    train,
    training_data,
    uncertainty,
)

__all__ = [
    "model_schema", "feature_schema", "training_data", "preprocessing", "uncertainty",
    "model_card", "train", "predict", "model_registry",
]
