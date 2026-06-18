"""Local **model registry** — save / load / list trained models under a safe, gitignored folder.

Each model lives in its own subfolder: ``<base>/<name>/model.joblib`` + ``model_card.json`` +
``meta.json`` (a small index record for fast listing without loading the artifact). The base dir
is a run's ``outputs/model_registry/`` (gitignored) — a **safe-path guard** refuses to write into
the source tree, ``data/raw``, or ``data/processed``, and an existing model is never overwritten
unless explicitly confirmed.

joblib is imported lazily inside save/load so the registry (and the listing helpers used by the
assistant) import without scikit-learn / joblib installed.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config
from . import model_schema

#: A model artifact must never land in these protected locations.
_FORBIDDEN_PARTS = ("data/raw", "data/processed", "flyash_phreeqc_ml")

MODEL_ARTIFACT = "model.joblib"
CARD_FILE = "model_card.json"
META_FILE = "meta.json"


class ModelRegistryError(Exception):
    """Base error for the model registry."""


class ModelExistsError(ModelRegistryError):
    """A model with this name already exists (and overwrite was not confirmed)."""


class ModelNotFoundError(ModelRegistryError):
    """No saved model with this name."""


def assert_safe_dir(path) -> Path:
    """Resolve ``path`` and refuse a protected location (source tree / raw / processed)."""
    p = Path(path).resolve()
    s = str(p).replace("\\", "/") + "/"
    for frag in _FORBIDDEN_PARTS:
        if f"/{frag}/" in s:
            raise ModelRegistryError(f"refusing to use a protected location for models: {p}")
    return p


def default_registry_dir() -> Path:
    """A project-level fallback registry (``outputs/model_registry/``, gitignored)."""
    return Path(config.PROJECT_ROOT) / "outputs" / "model_registry"


def _safe_name(name: str) -> str:
    import re
    slug = re.sub(r"[^0-9A-Za-z._-]+", "_", str(name).strip()).strip("_")
    if not slug:
        raise ModelRegistryError(f"model name {name!r} has no usable characters")
    return slug


class ModelRegistry:
    """A directory of trained models (one subfolder each)."""

    def __init__(self, base_dir):
        self.base = assert_safe_dir(base_dir)

    # --- locations ------------------------------------------------------- #
    def model_dir(self, name: str) -> Path:
        return self.base / _safe_name(name)

    def card_path(self, name: str) -> Path:
        return self.model_dir(name) / CARD_FILE

    def exists(self, name: str) -> bool:
        return (self.model_dir(name) / MODEL_ARTIFACT).exists()

    # --- write ----------------------------------------------------------- #
    def save(self, model: model_schema.TrainedModel, *, overwrite: bool = False) -> Path:
        """Persist ``model`` (artifact + card + meta). Refuses to overwrite unless confirmed."""
        import joblib

        name = model.name
        mdir = self.model_dir(name)
        if (mdir / MODEL_ARTIFACT).exists() and not overwrite:
            raise ModelExistsError(
                f"a model named {name!r} already exists — confirm overwrite to replace it.")
        mdir.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, mdir / MODEL_ARTIFACT)
        (mdir / CARD_FILE).write_text(json.dumps(model.card, indent=2), encoding="utf-8")
        (mdir / META_FILE).write_text(json.dumps(self._meta(model), indent=2), encoding="utf-8")
        return mdir

    @staticmethod
    def _meta(model: model_schema.TrainedModel) -> dict:
        return {
            "name": model.name, "target": model.target, "model_type": model.model_type,
            "model_family": model.model_family, "validation_status": model.validation_status,
            "source_type": model.source_type, "n_train": int(model.n_train),
            "n_validation": int(model.n_validation), "version": model.version,
            "date": model.created,
            "metrics": {k: model.metrics.get(k) for k in ("MAE", "RMSE", "R2", "method")},
        }

    # --- read ------------------------------------------------------------ #
    def load(self, name: str) -> model_schema.TrainedModel:
        import joblib

        path = self.model_dir(name) / MODEL_ARTIFACT
        if not path.exists():
            raise ModelNotFoundError(f"no saved model named {name!r}")
        return joblib.load(path)

    def model_card(self, name: str) -> dict:
        p = self.card_path(name)
        if not p.exists():
            raise ModelNotFoundError(f"no model card for {name!r}")
        return json.loads(p.read_text(encoding="utf-8"))

    def list_models(self) -> list:
        """Index records (from ``meta.json``) for every saved model — no artifact load."""
        if not self.base.exists():
            return []
        out = []
        for child in sorted(self.base.iterdir()):
            meta = child / META_FILE
            artifact = child / MODEL_ARTIFACT
            if not artifact.exists():
                continue
            try:
                rec = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
            except (json.JSONDecodeError, OSError):
                rec = {}
            rec.setdefault("name", child.name)
            out.append(rec)
        return out

    def delete(self, name: str) -> None:
        import shutil

        mdir = self.model_dir(name)
        if mdir.exists():
            shutil.rmtree(mdir)


# --------------------------------------------------------------------------- #
# Lightweight queries (used by the assistant to decide whether to offer the ML surrogate).
# These never load a model artifact — they read the index records only.
# --------------------------------------------------------------------------- #
def available_targets(base_dir) -> set:
    """The set of targets that have at least one saved model under ``base_dir`` (safe, cheap)."""
    try:
        reg = ModelRegistry(base_dir)
        return {r.get("target") for r in reg.list_models() if r.get("target")}
    except ModelRegistryError:
        return set()


def has_strength_model(base_dir, *, include_demo: bool = True) -> bool:
    """True if a trained mechanical-property (composite) model exists under ``base_dir``.

    ``include_demo=False`` ignores demo models (so the assistant offers only a real surrogate).
    """
    try:
        reg = ModelRegistry(base_dir)
    except ModelRegistryError:
        return False
    for rec in reg.list_models():
        if rec.get("target") not in model_schema.SUPPORTED_TARGETS:
            continue
        if not include_demo and rec.get("validation_status") == model_schema.VALIDATION_DEMO:
            continue
        return True
    return False
