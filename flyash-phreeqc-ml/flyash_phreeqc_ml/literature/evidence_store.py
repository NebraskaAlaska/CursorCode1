"""Read-only compatibility for historical per-run evidence JSONL plus safe exports.

Phase 3 evidence is written only as a durable ``ArtifactRecord`` through
``literature.evidence_review``.  Historical JSONL under a run's ``outputs/literature`` directory
remains readable and exportable, but this module deliberately refuses to append or replace it; a
second writable evidence authority would make review state and revision history ambiguous.

The structured-content boundary is shared by compatibility exports and the durable review service.
It rejects source-document copies and raw model output while retaining concise structured notes,
claims, provenance, values, units, and conditions.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import evidence_schema as E

EVIDENCE_SUBDIR = "literature"


class MissingProvenanceError(ValueError):
    """Raised when an evidence row has no source/provenance (a value with no source is not evidence)."""


class ForbiddenEvidenceContentError(ValueError):
    """Raised when a payload attempts to persist source text or a raw model response."""


class LegacyEvidenceReadOnlyError(ValueError):
    """Raised when code attempts to write the superseded per-run JSONL authority."""


_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_FIELD_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")
_FORBIDDEN_COMPACT_FIELDS = {
    # Abstract/source-document copies.  Summary/claim/note fields are intentionally absent.
    "abstract", "fullabstract", "entireabstract", "verbatimabstract", "rawabstract",
    "abstracttext", "abstractbody", "abstractcontent", "abstractcopy", "abstracttranscript",
    "paperabstract", "fulltext", "fullpaper", "entirepaper", "verbatimpaper",
    "fullpapertext", "papertext", "paperbody", "papercontent", "papercopy",
    "papertranscript", "fullarticle", "entirearticle", "articletext", "articlebody",
    "articlecontent", "articlecopy", "articletranscript", "fullmanuscript",
    "manuscripttext", "manuscriptbody", "manuscriptcontent", "manuscriptcopy",
    "sourcetext", "sourcebody", "sourcecontent", "sourcecopy", "sourcedocument",
    "sourcedocumenttext", "documenttext", "documentbody", "documentcontent", "documentcopy",
    "documenttranscript", "fulldocument", "entiredocument", "verbatimdocument",
    "publicationtext", "publicationbody", "publicationcontent", "publicationcopy",
    "fullpublication", "pdftext", "pdfbody", "pdfcontent", "pdfcopy", "fullpdf",
    "fullcontent", "entirecontent", "verbatimcontent", "copiedtext", "copiedcontent",
    # Raw model/agent output, including common completion/generation aliases.
    "rawresponse", "rawoutput", "rawcompletion", "rawgeneration", "rawtranscript",
    "rawreasoning", "rawthoughts", "rawtrace", "rawllmresponse", "rawllmoutput",
    "rawllmcompletion", "rawllmgeneration", "rawmodelresponse", "rawmodeloutput",
    "rawmodelcompletion", "rawmodelgeneration", "rawairesponse", "rawaioutput",
    "rawaicompletion", "rawaigeneration", "llmresponse", "llmoutput", "llmcompletion",
    "llmgeneration", "llmtranscript", "llmmessage", "modelresponse", "modeloutput",
    "modelcompletion", "modelgeneration", "modeltranscript", "modelmessage", "airesponse",
    "aioutput", "aicompletion", "aigeneration", "aitranscript", "aimessage",
    "assistantresponse", "assistantoutput", "assistantcompletion", "assistantgeneration",
    "assistanttranscript", "assistantmessage", "completiontext", "generationtext",
    "responsetext", "outputtext", "chainofthought", "hiddenreasoning", "reasoningtrace",
    "privateanalysis", "internalthoughts", "rawllm", "llmraw", "rawmodel", "modelraw",
    "rawai", "airaw", "rawassistant", "assistantraw", "rawagent", "agentraw",
    "completionpayload", "completionchoices", "generationpayload", "responsepayload",
    "outputpayload", "chatcompletion", "chatresponse", "chatoutput", "chatmessages",
}
_SOURCE_DOCUMENT_TOKENS = {
    "abstract", "paper", "article", "manuscript", "document", "publication", "pdf", "source",
}
_SOURCE_COPY_TOKENS = {
    "text", "body", "content", "copy", "transcript", "html", "markdown", "xml",
    "blob", "bytes",
}
_MODEL_TOKENS = {"llm", "model", "ai", "assistant", "agent", "chat", "chatbot"}
_MODEL_OUTPUT_TOKENS = {
    "response", "output", "completion", "generation", "transcript", "message",
    "reasoning", "thought", "thoughts", "trace", "analysis", "text", "content", "body",
    "choices", "delta",
}
_STRUCTURED_QUALIFIER_TOKENS = {
    "summary", "note", "label", "id", "identifier", "record", "reference", "time",
    "count", "score", "status", "scope", "hash", "type", "unit", "units",
}
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")
PACKAGE_SCHEMA_VERSION = 1
MAX_EVIDENCE_STRING_CHARS = 400
MAX_EVIDENCE_STRING_WORDS = 50
MAX_EVIDENCE_CONTAINER_ITEMS = 64
MAX_EVIDENCE_NESTING_DEPTH = 6
MAX_EVIDENCE_RECORD_BYTES = 16 * 1024
_RAW_CONTENT_PREFIX_RE = re.compile(
    r"^\s*(?:full\s+(?:paper|abstract|text)|paper\s+text|article\s+text|"
    r"abstract|raw\s+(?:llm|model|ai|assistant|agent)(?:\s+(?:response|output|"
    r"completion|generation|transcript))?|chain[-\s]+of[-\s]+thought)\s*[:\-]",
    re.IGNORECASE,
)


def _field_parts(field_name) -> tuple[str, ...]:
    separated = _CAMEL_BOUNDARY_RE.sub("_", str(field_name).strip())
    return tuple(part.lower() for part in _FIELD_SEPARATOR_RE.split(separated) if part)


def _is_forbidden_field(parts: tuple[str, ...], ancestors: tuple[str, ...]) -> bool:
    compact = "".join(parts)
    if compact in _FORBIDDEN_COMPACT_FIELDS:
        return True
    tokens = set(parts)
    if _SOURCE_DOCUMENT_TOKENS & tokens and _SOURCE_COPY_TOKENS & tokens:
        return True
    if ({"source", "document"}.issubset(tokens)
            and not _STRUCTURED_QUALIFIER_TOKENS & tokens):
        return True
    if (_MODEL_TOKENS & tokens and _MODEL_OUTPUT_TOKENS & tokens
            and (not _STRUCTURED_QUALIFIER_TOKENS & tokens
                 or {"text", "content", "body", "transcript"} & tokens)):
        return True
    if "raw" in tokens and _MODEL_OUTPUT_TOKENS & tokens:
        return True
    if "chain" in tokens and ({"thought", "thoughts"} & tokens):
        return True
    if ({"hidden", "internal", "private"} & tokens
            and {"reasoning", "analysis", "thought", "thoughts", "trace"} & tokens):
        return True
    # Catch split nested aliases such as ``llm: {response: ...}`` and
    # ``raw: {completion: ...}`` without banning scientific ``response_time`` fields.
    if ancestors:
        adjacent = ancestors[-1] + compact
        if adjacent in _FORBIDDEN_COMPACT_FIELDS:
            return True
    return False


def _looks_like_raw_model_envelope(value: dict) -> bool:
    """Recognize common structured completion/message envelopes, not scientific fields."""
    by_compact_key = {"".join(_field_parts(key)): child for key, child in value.items()}
    keys = set(by_compact_key)
    choices = by_compact_key.get("choices")
    if isinstance(choices, list):
        if keys & {"model", "object", "usage", "created", "systemfingerprint"}:
            return True
        for choice in choices:
            if isinstance(choice, dict) and {
                    "message", "delta", "finishreason"} & {
                        "".join(_field_parts(key)) for key in choice}:
                return True
    messages = by_compact_key.get("messages")
    if isinstance(messages, list) and any(
            isinstance(message, dict)
            and {"role", "content"}.issubset({
                "".join(_field_parts(key)) for key in message})
            for message in messages):
        return True
    role = str(by_compact_key.get("role") or "").strip().lower()
    if role in {"assistant", "model"} and "content" in keys \
            and keys & {"model", "usage", "stopreason", "stopsequence", "type"}:
        return True
    candidates = by_compact_key.get("candidates")
    if isinstance(candidates, list) and keys & {
            "usagemetadata", "modelversion", "promptfeedback"}:
        return True
    return False


def _assert_structured_only(
    value,
    path: str = "evidence",
    field_chain: tuple[str, ...] = (),
    *,
    enforce_bounds: bool = False,
    depth: int = 0,
) -> None:
    if enforce_bounds and depth > MAX_EVIDENCE_NESTING_DEPTH:
        raise ForbiddenEvidenceContentError(
            f"{path} exceeds the structured evidence nesting limit")
    if isinstance(value, dict):
        if enforce_bounds and len(value) > MAX_EVIDENCE_CONTAINER_ITEMS:
            raise ForbiddenEvidenceContentError(
                f"{path} exceeds the structured evidence field limit")
        if _looks_like_raw_model_envelope(value):
            raise ForbiddenEvidenceContentError(
                f"{path} looks like a raw model-response envelope")
        for key, child in value.items():
            parts = _field_parts(key)
            if _is_forbidden_field(parts, field_chain):
                raise ForbiddenEvidenceContentError(
                    f"{path}.{key} is not permitted; store structured evidence, not source text "
                    "or a raw model response")
            _assert_structured_only(
                child, f"{path}.{key}", field_chain + ("".join(parts),),
                enforce_bounds=enforce_bounds, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if enforce_bounds and len(value) > MAX_EVIDENCE_CONTAINER_ITEMS:
            raise ForbiddenEvidenceContentError(
                f"{path} exceeds the structured evidence item limit")
        for index, child in enumerate(value):
            _assert_structured_only(
                child, f"{path}[{index}]", field_chain,
                enforce_bounds=enforce_bounds, depth=depth + 1)
    elif isinstance(value, str) and enforce_bounds:
        if len(value) > MAX_EVIDENCE_STRING_CHARS \
                or len(re.findall(r"\S+", value)) > MAX_EVIDENCE_STRING_WORDS:
            raise ForbiddenEvidenceContentError(
                f"{path} exceeds the concise structured-text limit")
        if _RAW_CONTENT_PREFIX_RE.search(value):
            raise ForbiddenEvidenceContentError(
                f"{path} looks like copied source text or raw model output")
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            lowered = stripped.lower()
            if ((('"choices"' in lowered or '"messages"' in lowered)
                    and ('"content"' in lowered or '"role"' in lowered))
                    or ('"assistant"' in lowered and '"content"' in lowered)):
                raise ForbiddenEvidenceContentError(
                    f"{path} looks like a raw model-response envelope")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")


def validate_structured_payload(value, *, allowed_top_level_fields=None) -> None:
    """Validate one bounded evidence record at the durable/export boundary.

    ``allowed_top_level_fields`` lets the durable review authority enforce its closed
    schema while compatibility exports may retain known historical columns. Bounds
    apply per record, so exporting a collection does not weaken the record boundary.
    """
    if allowed_top_level_fields is not None:
        if not isinstance(value, dict):
            raise ForbiddenEvidenceContentError(
                "durable evidence must be a structured object")
        unknown = set(value) - set(allowed_top_level_fields)
        if unknown:
            raise ForbiddenEvidenceContentError(
                f"unsupported durable evidence fields: {sorted(unknown)}")
    _assert_structured_only(value, enforce_bounds=True)
    encoded = _canonical_json(value)
    if len(encoded.encode("utf-8")) > MAX_EVIDENCE_RECORD_BYTES:
        raise ForbiddenEvidenceContentError(
            "evidence record exceeds the bounded structured-payload limit")


def _canonical_json(value, *, indent: int | None = None) -> str:
    _assert_structured_only(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                          if indent is None else None, indent=indent, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"evidence is not finite JSON-safe structured data: {exc}") from exc


# --------------------------------------------------------------------------- #
# Historical paths (read-only compatibility; never a new measured/source authority)
# --------------------------------------------------------------------------- #
_FORBIDDEN_PARTS = ("data/raw", "data/processed", "flyash_phreeqc_ml")


def assert_safe_path(path) -> Path:
    p = Path(path).resolve()
    s = str(p).replace("\\", "/")
    if any(f"/{frag}/" in s + "/" for frag in _FORBIDDEN_PARTS):
        raise ValueError(f"protected location is not an evidence compatibility path: {p}")
    return p


def evidence_path(outputs_dir, schema_kind: str) -> Path:
    """Return a historical ``<outputs_dir>/literature/evidence_<schema>.jsonl`` path."""
    if schema_kind not in E.SCHEMA_KINDS:
        raise ValueError(f"unsupported evidence schema_kind: {schema_kind!r}")
    d = Path(outputs_dir) / EVIDENCE_SUBDIR
    return d / f"evidence_{schema_kind}.jsonl"


# --------------------------------------------------------------------------- #
# Read-only JSONL compatibility (tolerant reader; legacy writers fail closed)
# --------------------------------------------------------------------------- #
def add_evidence(path, evidence) -> Path:
    """Refuse legacy JSONL appends; create evidence through ``evidence_review`` instead."""
    raise LegacyEvidenceReadOnlyError(
        "historical per-run evidence JSONL is read-only; create a durable evidence artifact")


def save_evidence(path, evidences) -> Path:
    """Refuse legacy JSONL replacement; create evidence revisions through ``evidence_review``."""
    raise LegacyEvidenceReadOnlyError(
        "historical per-run evidence JSONL is read-only; create durable evidence artifacts")


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
def _flat_export_row(row: dict) -> dict:
    flat = dict(row)
    provenance = flat.get("provenance") if isinstance(flat.get("provenance"), dict) else {}
    location = flat.get("source_location") \
        if isinstance(flat.get("source_location"), dict) else {}
    for key in ("source", "doi", "title", "url", "authors", "year", "query"):
        flat.setdefault(f"citation_{key}", provenance.get(key))
    for key in E.SourceLocation.__dataclass_fields__:
        flat.setdefault(f"source_{key}", location.get(key))
    if not flat.get("source_location_summary"):
        flat["source_location_summary"] = E.SourceLocation(
            **{key: location.get(key) for key in E.SourceLocation.__dataclass_fields__}).summary
    return flat


def _csv_cell(value):
    if isinstance(value, (list, dict)):
        value = _canonical_json(value)
    # Protect only untrusted text cells. Numeric values, including negative
    # scientific numbers, remain numeric and unchanged.
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(_CSV_FORMULA_PREFIXES):
            return "'" + value
    return value


def to_csv(rows, schema_kind: str) -> str:
    """Render evidence rows (list of dicts from ``read_evidence`` / ``to_row``) as CSV text."""
    import csv
    import io
    columns = list(E.columns_for(schema_kind))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = _flat_export_row(dict(row))
        validate_structured_payload(flat)
        writer.writerow({c: _csv_cell(flat.get(c, "")) for c in columns})
    return buf.getvalue()


def export_csv(path, schema_kind: str) -> str:
    """Read the per-schema JSONL store and return its CSV text (empty header if no rows)."""
    return to_csv(read_evidence(path), schema_kind)


def to_json(rows) -> str:
    """Deterministic structured JSON export; never includes source text/model responses."""
    normalized = [dict(row) for row in rows]
    for row in normalized:
        validate_structured_payload(row)
    return _canonical_json(normalized, indent=2) + "\n"


def to_package(rows, schema_kind: str, *, metadata: dict | None = None) -> str:
    """Deterministic provenance-rich evidence package."""
    normalized = [dict(row) for row in rows]
    for row in normalized:
        validate_structured_payload(row)
    package_metadata = dict(metadata or {})
    validate_structured_payload(package_metadata)
    package = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "record_type": "evidence",
        "schema_kind": schema_kind,
        "metadata": package_metadata,
        "record_count": len(normalized),
        "records": normalized,
    }
    return _canonical_json(package, indent=2) + "\n"
