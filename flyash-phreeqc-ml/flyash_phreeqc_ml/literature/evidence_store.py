"""Per-run **evidence library** store (pure file I/O; no AI, no network, no result path).

Saves curated evidence rows to a gitignored per-run JSONL (under ``experiments/<run>/outputs/
literature/``) and exports a CSV — a dataset a *future* ML / surrogate model could train on. It
enforces the schema's honesty rules at the boundary:

* **Provenance required** — :func:`add_evidence` raises :class:`MissingProvenanceError` for a row
  with no DOI and no title+source. A value with no source is not evidence.
* **Structured values only** — it stores the row's ``to_row()`` (the extracted fields + the
  citation metadata), **never** the paper's abstract / full text (no copyrighted text is stored)
  and **never** a raw model response.
* **Safe location only** — :func:`assert_safe_path` refuses to write into ``data/raw`` /
  ``data/processed`` / the source tree; evidence lives only under a run's ``outputs/``.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import evidence_schema as E

EVIDENCE_SUBDIR = "literature"


class MissingProvenanceError(ValueError):
    """Raised when an evidence row has no source/provenance (a value with no source is not evidence)."""


# --------------------------------------------------------------------------- #
# Safe paths (evidence is a gitignored run output — never measured data / source tree)
# --------------------------------------------------------------------------- #
_FORBIDDEN_PARTS = ("data/raw", "data/processed", "flyash_phreeqc_ml")


def assert_safe_path(path) -> Path:
    p = Path(path).resolve()
    s = str(p).replace("\\", "/")
    if any(f"/{frag}/" in s + "/" for frag in _FORBIDDEN_PARTS):
        raise ValueError(f"refusing to write evidence into a protected location: {p}")
    return p


def evidence_path(outputs_dir, schema_kind: str) -> Path:
    """``<outputs_dir>/literature/evidence_<schema>.jsonl`` (the per-run store for one schema)."""
    d = Path(outputs_dir) / EVIDENCE_SUBDIR
    return d / f"evidence_{schema_kind}.jsonl"


# --------------------------------------------------------------------------- #
# Read / write (append-friendly JSONL; tolerant reader)
# --------------------------------------------------------------------------- #
def add_evidence(path, evidence) -> Path:
    """Append one evidence row (requires provenance). Returns the path written."""
    if not getattr(evidence, "has_provenance", False):
        raise MissingProvenanceError(
            "evidence row has no source/provenance (a DOI or a title + source is required).")
    p = assert_safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(evidence.to_row(), ensure_ascii=False) + "\n")
    return p


def save_evidence(path, evidences) -> Path:
    """Write a list of evidence rows (each must have provenance), replacing the file."""
    rows = []
    for ev in evidences:
        if not getattr(ev, "has_provenance", False):
            raise MissingProvenanceError("an evidence row has no source/provenance.")
        rows.append(ev.to_row())
    p = assert_safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return p


def read_evidence(path) -> list:
    """Read evidence rows (list of dicts); tolerant of malformed lines, missing file → []."""
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
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


# --------------------------------------------------------------------------- #
# CSV export (structured values + citation; never abstracts / full text)
# --------------------------------------------------------------------------- #
def to_csv(rows, schema_kind: str) -> str:
    """Render evidence rows (list of dicts from ``read_evidence`` / ``to_row``) as CSV text."""
    import csv
    import io
    columns = list(E.columns_for(schema_kind))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        for key, val in list(flat.items()):
            if isinstance(val, (list, dict)):
                flat[key] = json.dumps(val, ensure_ascii=False)
        writer.writerow({c: flat.get(c, "") for c in columns})
    return buf.getvalue()


def export_csv(path, schema_kind: str) -> str:
    """Read the per-schema JSONL store and return its CSV text (empty header if no rows)."""
    return to_csv(read_evidence(path), schema_kind)
