"""Optional LLM helper that *proposes* interpretations of messy uploaded files.

This is a strictly **opt-in, suggestion-only** layer for the Data tab's generic
importer. It never decides a mapping or a validation status, never saves anything,
and never touches the pipeline: every proposal it returns flows back into the
existing review-and-confirm UI, where the human accepts or rejects it.

Design rules (all enforced here):

* **Disabled by default.** Every AI call requires ``ANTHROPIC_API_KEY`` in the
  environment *and* the ``anthropic`` SDK installed. If either is missing,
  :func:`is_enabled` returns ``False`` and the public functions degrade to an
  empty / rule-only result — the importer works fully without this module.
* **Minimal data leaves the machine.** Only column headers + the first
  :data:`MAX_SAMPLE_ROWS` rows (or a bounded batch of sample ids) are ever sent —
  never the full dataset. The app shows a one-time per-session notice and a
  consent checkbox before any of these functions are called.
* **Rule-first.** :func:`parse_sample_names` parses ids with the dataset
  profile's conventions and only sends the ids the rules *couldn't* parse to the
  model, so fully-parseable ids never hit the API.
* **Defensive parsing.** The model is asked for strict JSON; we strip code
  fences and parse defensively, falling back to an empty result on any failure
  (bad JSON, an API error, a refusal). Nothing here raises into the app.
* **No logging of uploaded content.** This module never prints or logs the
  headers, rows, or sample ids it receives.

The Anthropic SDK is imported lazily inside :func:`_resolve_client` so importing
this module (and the whole package) never requires ``anthropic`` to be installed.
"""
from __future__ import annotations

import json
import re

from .. import profiles, replicates, config
from . import client as ai_client      # shared, key-safe client wrapper
from . import config as ai_config      # shared AI configuration authority

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# Key / model / provider resolution now lives in ai/config.py + ai/client.py. The names
# below are re-exported (and the helpers delegate) so this module stays the stable import
# surface for assistant.py + literature.py and nothing downstream needs to change.
API_KEY_ENV = ai_config.API_KEY_ENV
MODEL_ENV = ai_config.MODEL_ENV
DEFAULT_MODEL = ai_config.DEFAULT_MODEL

# Hard caps on what we send — "minimum needed", never the full dataset.
MAX_SAMPLE_ROWS = 10      # rows of a preview sent to the model
MAX_CELL_CHARS = 120      # truncate long cells
MAX_SAMPLE_IDS = 100      # ids sent to the model in one batch

# Per-row metadata provenance (written to the saved frame after confirmation).
SOURCE_RULE = "rule"            # parsed by the profile's rules (no AI)
SOURCE_AI = "ai"                # proposed by the model
SOURCE_MANUAL = "manual"        # entered/kept by the user
METADATA_PROVENANCE_COLUMN = "metadata_provenance"
RULE_CONFIDENCE = 0.99
# source keyword -> the value stored in the metadata_provenance column.
_PROVENANCE_BY_SOURCE = {
    SOURCE_RULE: "rule",
    SOURCE_AI: "ai-confirmed",   # AI proposed it AND the user confirmed the save
    SOURCE_MANUAL: "manual",
    None: "manual",
}

ALLOWED_SHEET_CLASSES = ("pH", "ICP", "metadata", "other")
ALLOWED_UNIT_GUESSES = ("mM", "mg/L", "ppm", "ppb", None)

# Shown once per session in the AI-assist expander before anything is sent.
DATA_LEAVES_MACHINE_NOTICE = (
    "These optional suggestions send your column headers and the first "
    f"{MAX_SAMPLE_ROWS} rows (or a batch of sample names) to the Anthropic API — "
    "data leaves this machine for this feature only. Nothing is saved or sent "
    "anywhere else, and every suggestion lands in the review step below for you "
    "to accept or reject."
)
CONSENT_LABEL = "I understand and allow sending headers + a preview to the API for AI suggestions."


# --------------------------------------------------------------------------- #
# Enablement + client resolution
# --------------------------------------------------------------------------- #
def is_enabled() -> bool:
    """True only when an API key *and* the ``anthropic`` SDK are available.

    Delegates to the shared AI config so the key/model/provider rules live in one place.
    """
    return ai_config.is_enabled()


def _model(model: str | None = None) -> str:
    """Resolve the model id (arg > UI override > env > Streamlit secret > default)."""
    return ai_config.resolve_model(model)


def _resolve_client(client):
    """Return a usable client: the injected one, a real one, or ``None`` (disabled).

    Tests inject a fake client (returned as-is); the app passes ``None`` and a real client
    is built only when enabled. Delegates to the shared, key-safe client wrapper; any
    construction failure degrades to ``None``.
    """
    return ai_client.resolve_client(client)


# --------------------------------------------------------------------------- #
# Defensive JSON handling
# --------------------------------------------------------------------------- #
def _message_text(resp) -> str:
    """Concatenate the text blocks of a Messages API response (defensive)."""
    parts: list[str] = []
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):      # drop ``` or ```json
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _parse_json(text: str):
    """Parse model output as JSON, tolerating code fences and surrounding prose.

    Returns the parsed object, or ``None`` on any failure (the caller falls back).
    """
    t = _strip_fences(text)
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception:
        pass
    # Last resort: grab the outermost array/object substring and try that.
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = t.find(open_c), t.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


def _request_json(client, *, model: str, system: str, user: str, max_tokens: int):
    """Send one request and return parsed JSON, or ``None`` on any failure.

    Wrapped so API errors / refusals / bad JSON never propagate into the app.
    """
    try:
        resp = client.messages.create(
            model=_model(model),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception:
        return None
    return _parse_json(_message_text(resp))


def _truncate_rows(sample_rows) -> list[list[str]]:
    """Cap rows to MAX_SAMPLE_ROWS and each cell to MAX_CELL_CHARS (minimise payload)."""
    out: list[list[str]] = []
    for row in list(sample_rows or [])[:MAX_SAMPLE_ROWS]:
        cells = []
        for cell in row:
            s = "" if cell is None else str(cell)
            cells.append(s[:MAX_CELL_CHARS])
        out.append(cells)
    return out


# --------------------------------------------------------------------------- #
# 1) Sheet classification
# --------------------------------------------------------------------------- #
_SHEET_SYSTEM = (
    "You classify spreadsheet sheets for a geochemistry data importer. For each "
    "sheet you are given its name, headers, and a few sample rows. Decide whether "
    "the sheet most likely holds: 'pH' measurements, 'ICP' element concentrations, "
    "'metadata' (sample descriptions / conditions), or 'other'. Respond with ONLY a "
    "JSON array, one object per sheet, each: "
    '{"sheet": <name>, "likely_content": "pH|ICP|metadata|other", "evidence": <short reason>}. '
    "No prose, no code fences."
)


def classify_sheets(sheet_previews, *, client=None, model=None) -> list[dict]:
    """Propose a content class for each sheet (suggestion only).

    ``sheet_previews`` is a list of ``{"sheet", "headers", "rows"}`` dicts. Returns
    ``[{"sheet", "likely_content", "evidence"}]`` (``likely_content`` is always one
    of :data:`ALLOWED_SHEET_CLASSES`). Returns ``[]`` when disabled or on failure.
    """
    previews = list(sheet_previews or [])
    if not previews:
        return []
    client = _resolve_client(client)
    if client is None:
        return []

    payload = [
        {
            "sheet": str(p.get("sheet", "")),
            "headers": [str(h) for h in (p.get("headers") or [])],
            "rows": _truncate_rows(p.get("rows")),
        }
        for p in previews
    ]
    user = "Classify these sheets:\n" + json.dumps(payload, ensure_ascii=False)
    parsed = _request_json(client, model=model, system=_SHEET_SYSTEM, user=user, max_tokens=1500)
    if not isinstance(parsed, list):
        return []

    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        cls = str(item.get("likely_content", "")).strip()
        if cls not in ALLOWED_SHEET_CLASSES:
            cls = "other"
        out.append({
            "sheet": str(item.get("sheet", "")),
            "likely_content": cls,
            "evidence": str(item.get("evidence", "")).strip(),
        })
    return out


# --------------------------------------------------------------------------- #
# 2) Column-mapping proposal
# --------------------------------------------------------------------------- #
_COLMAP_SYSTEM = (
    "You help map the columns of an uploaded experimental table onto a target "
    "schema. You are given the uploaded headers, a few sample rows, and the list "
    "of allowed target column names. For each uploaded column propose the best "
    "matching target (or null if none fits), a unit guess for chemistry columns "
    "(one of 'mg/L','ppm','ppb','mM', or null), a confidence in [0,1], and a short "
    "reason. Respond with ONLY a JSON array, one object per uploaded column: "
    '{"source_col": <header>, "target_col": <target or null>, '
    '"unit_guess": <unit or null>, "confidence": <0..1>, "evidence": <short reason>}. '
    "No prose, no code fences."
)


def default_target_schema(profile=None) -> list[str]:
    """The target schema the importer maps onto (release columns from config).

    Sourced from ``config.EXPERIMENTAL_RELEASE_COLUMNS`` (the dataset profile's
    schema), so the proposal targets exactly the columns the importer accepts.
    """
    return list(config.EXPERIMENTAL_RELEASE_COLUMNS)


def _clamp_confidence(value) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, c))


def propose_column_mapping(headers, sample_rows, target_schema, *,
                           client=None, model=None) -> list[dict]:
    """Propose a column mapping (suggestion only).

    Returns ``[{"source_col","target_col","unit_guess","confidence","evidence"}]``,
    restricted to real ``headers`` and ``target_schema`` values. Returns ``[]`` when
    disabled or on any parse/API failure.
    """
    headers = [str(h) for h in (headers or [])]
    schema = [str(t) for t in (target_schema or [])]
    if not headers:
        return []
    client = _resolve_client(client)
    if client is None:
        return []

    payload = {
        "headers": headers,
        "sample_rows": _truncate_rows(sample_rows),
        "target_schema": schema,
    }
    user = "Map these columns:\n" + json.dumps(payload, ensure_ascii=False)
    parsed = _request_json(client, model=model, system=_COLMAP_SYSTEM, user=user, max_tokens=2000)
    if not isinstance(parsed, list):
        return []

    header_set, schema_set = set(headers), set(schema)
    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        src = str(item.get("source_col", "")).strip()
        if src not in header_set:
            continue
        tgt = item.get("target_col")
        tgt = str(tgt).strip() if tgt not in (None, "") else None
        if tgt is not None and tgt not in schema_set:
            tgt = None
        unit = item.get("unit_guess")
        unit = str(unit).strip() if unit not in (None, "") else None
        if unit not in ALLOWED_UNIT_GUESSES:
            unit = None
        out.append({
            "source_col": src,
            "target_col": tgt,
            "unit_guess": unit,
            "confidence": _clamp_confidence(item.get("confidence")),
            "evidence": str(item.get("evidence", "")).strip(),
        })
    return out


# --------------------------------------------------------------------------- #
# 3) Sample-name parsing (rule-first; AI only for the leftovers)
# --------------------------------------------------------------------------- #
_NAME_FIELDS = ("leachant", "concentration", "condition_code", "time_min", "replicate")
# Core fields that must all be present for a sample id to count as rule-parsed.
_RULE_CORE = ("leachant", "concentration", "condition_code", "time_min")

_LEACHANT_RE = re.compile(r"(?P<l>NaOH|KOH|HCl)\s*(?P<c>\d+(?:\.\d+)?)\s*M", re.IGNORECASE)
_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*min", re.IGNORECASE)


def _condition_code_regex(profile):
    codes = [str(c) for c in (getattr(profile, "condition_codes", None) or {})]
    if not codes:
        return None
    alt = "|".join(re.escape(c) for c in sorted(codes, key=len, reverse=True))
    return re.compile(r"(?:^|[-_ ])(" + alt + r")(?:[-_ ]|$)", re.IGNORECASE)


def _rule_parse(sample_id: str, profile) -> dict | None:
    """Parse one sample id with the profile's conventions, or ``None`` if it can't.

    Tries the profile's full ``sample_id_pattern`` first (generic datasets), then
    the fly-ash token conventions (``CFA-NaOH{M}M-LS{ratio}-{min}min-{CO2}-R{rep}``
    and common variants). Returns a fields dict only when all of
    :data:`_RULE_CORE` are found, so partially-named ids defer to the model.
    """
    s = str(sample_id or "").strip()
    if not s:
        return None

    pattern = getattr(profile, "sample_id_pattern", None)
    if pattern:
        m = re.match(pattern, s, re.IGNORECASE)
        if m:
            gd = {k: v for k, v in m.groupdict().items() if v not in (None, "")}
            if gd:
                return gd

    fields: dict = {}
    leach = _LEACHANT_RE.search(s)
    if leach:
        fields["leachant"] = leach.group("l")
        fields["concentration"] = leach.group("c")
    code_re = _condition_code_regex(profile)
    if code_re:
        cm = code_re.search(s)
        if cm:
            fields["condition_code"] = cm.group(1).upper()
    tm = _TIME_RE.search(s)
    if tm:
        fields["time_min"] = tm.group(1)
    rep = replicates.parse_replicate_id(s)
    if rep:
        fields["replicate"] = rep

    if all(k in fields for k in _RULE_CORE):
        return fields
    return None


_NAMES_SYSTEM = (
    "You parse experimental sample names into structured fields for a "
    "geochemistry importer. For each sample id, extract whatever you can of: "
    "leachant (e.g. NaOH/HCl), concentration (molarity number), condition_code "
    "(a cup-cover / condition code such as OA/PF/GS when present), time_min "
    "(reaction time in minutes), replicate (e.g. R1). Use null for fields you "
    "cannot determine — never guess. Respond with ONLY a JSON array, one object "
    'per id: {"sample_id": <id>, "fields": {"leachant":..,"concentration":..,'
    '"condition_code":..,"time_min":..,"replicate":..}, "confidence": <0..1>, '
    '"note": <short reason>}. No prose, no code fences.'
)


def _unparsed_placeholder(sample_id: str) -> dict:
    return {"sample_id": sample_id, "fields": {}, "confidence": 0.0,
            "note": "could not parse from the name", "source": None}


def _ai_parse_sample_names(sample_ids, profile, *, client, model) -> dict:
    """Ask the model to parse the ids the rules couldn't. Returns {id: record}."""
    client = _resolve_client(client)
    if client is None:
        return {}
    batch = list(sample_ids)[:MAX_SAMPLE_IDS]
    known_codes = [str(c) for c in (getattr(profile, "condition_codes", None) or {})]
    payload = {"sample_ids": batch, "known_condition_codes": known_codes}
    user = "Parse these sample names:\n" + json.dumps(payload, ensure_ascii=False)
    parsed = _request_json(client, model=model, system=_NAMES_SYSTEM, user=user, max_tokens=3000)
    if not isinstance(parsed, list):
        return {}

    wanted = set(batch)
    out: dict = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("sample_id", "")).strip()
        if sid not in wanted:
            continue
        raw_fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        fields = {k: raw_fields.get(k) for k in _NAME_FIELDS
                  if raw_fields.get(k) not in (None, "")}
        out[sid] = {
            "sample_id": sid,
            "fields": fields,
            "confidence": _clamp_confidence(item.get("confidence")),
            "note": str(item.get("note", "")).strip() or "proposed by AI",
            "source": SOURCE_AI,
        }
    return out


def parse_sample_names(sample_ids, profile=None, *, client=None, model=None) -> list[dict]:
    """Parse sample names into fields, rule-first then AI for the leftovers.

    Returns one record per input id (order preserved):
    ``{"sample_id", "fields", "confidence", "note", "source"}`` where ``source`` is
    :data:`SOURCE_RULE`, :data:`SOURCE_AI`, or ``None`` (unparsed). Ids the rules
    fully parse are **never** sent to the model. With AI disabled, rule parsing
    still runs and the leftovers come back as unparsed placeholders.
    """
    profile = profile or profiles.default_dataset_profile()
    ids = [str(s) for s in (sample_ids or [])]
    results: dict = {}
    unparsed: list[str] = []

    for sid in ids:
        fields = _rule_parse(sid, profile)
        if fields is not None:
            results[sid] = {"sample_id": sid, "fields": fields,
                            "confidence": RULE_CONFIDENCE, "note": "parsed by rule",
                            "source": SOURCE_RULE}
        else:
            unparsed.append(sid)

    if unparsed:
        ai_rows = _ai_parse_sample_names(unparsed, profile, client=client, model=model)
        for sid in unparsed:
            results[sid] = ai_rows.get(sid) or _unparsed_placeholder(sid)

    return [results[sid] for sid in ids]


# --------------------------------------------------------------------------- #
# Provenance tagging (applied to the saved frame after the user confirms)
# --------------------------------------------------------------------------- #
def provenance_value(source) -> str:
    """Map a per-row ``source`` keyword to its ``metadata_provenance`` value."""
    return _PROVENANCE_BY_SOURCE.get(source, "manual")


def build_provenance_column(sources) -> list[str]:
    """Vectorised :func:`provenance_value` over a list of source keywords."""
    return [provenance_value(s) for s in (sources or [])]
