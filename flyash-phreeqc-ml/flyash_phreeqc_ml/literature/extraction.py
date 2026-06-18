"""AI extraction of structured evidence from a paper's abstract / text (the only AI module here).

Uses AI **only** to read text a paper actually provides and return structured values — every value
cites the paper (the row's provenance), **missing values stay ``None``**, uncertain values get low
confidence, conflicting values are flagged, and **no hidden condition is inferred**. An abstract is
treated as *abstract-only* (confidence-capped, never full experimental data). With no AI it returns
an **empty** evidence row with an honest status — it **never fabricates** a value, and it **never
stores the raw model response** (only the validated structured fields).

This is the single AI-touching module in :mod:`flyash_phreeqc_ml.literature`; it imports the
shared key-safe client (like ``ai/scenario_parser``) and runs nothing else.
"""
from __future__ import annotations

from ..ai import client as ai_client
from ..ai import config as ai_config
from ..ai.import_assist import _message_text, _parse_json
from . import evidence_schema as E

MAX_TOKENS = 1200

# Per-session consent (same spirit as the other AI features).
EXTRACTION_DATA_NOTICE = (
    "Extraction sends the selected paper's title + abstract (already-public bibliographic text) to "
    "the Anthropic API to read structured values from it — no measured data, no secrets, no files. "
    "Extracted values are suggestions with the paper's citation + a confidence; they are never "
    "treated as measured truth and never enter a calculation.")

_NUMERIC_LEACHING = ("concentration_M", "solid_mass_g", "liquid_volume_mL", "ls_ratio", "time_min",
                     "temperature_C", "pH")
_TEXT_LEACHING = ("material", "material_class", "composition", "leachant", "analytical_method",
                  "filtration")
_NUMERIC_COMPOSITE = ("water_binder_ratio", "compressive_strength_MPa", "flexural_strength_MPa",
                      "density_kg_m3", "water_absorption_pct")
_TEXT_COMPOSITE = ("material_binder", "plastic_type", "plastic_form", "plastic_particle_size",
                   "plastic_dosage", "activator_cement_content", "curing_time", "specimen_geometry",
                   "durability_observations")


# --------------------------------------------------------------------------- #
# Prompt (extraction only; no fabrication, no inference, JSON)
# --------------------------------------------------------------------------- #
def _system_prompt(schema_kind: str) -> str:
    if schema_kind == E.SCHEMA_LEACHING:
        fields = (f"numeric (null if not stated): {', '.join(_NUMERIC_LEACHING)}; "
                  f"text: {', '.join(_TEXT_LEACHING)}; "
                  f"element_values_mM: an object mapping any of {', '.join(E.LEACHING_ELEMENTS)} to "
                  "its reported dissolved concentration in mM (null if not reported); "
                  "elements_measured: the list of elements the paper measured")
    else:
        fields = (f"numeric (null if not stated): {', '.join(_NUMERIC_COMPOSITE)}; "
                  f"text: {', '.join(_TEXT_COMPOSITE)}")
    return f"""\
You extract STRUCTURED experimental values from a single materials-science paper's title +
abstract. You ONLY report what the text states. You NEVER invent, guess, or infer a hidden
condition, and you NEVER add a value the text does not give.

Rules:
- Missing / not-stated value -> null. Never 0, never a guess.
- If the text is an ABSTRACT (not full methods), set extraction_scope to "abstract" and keep
  confidence <= 0.45 — an abstract is not full experimental data.
- If the paper reports conflicting values for a field, list that field name in "conflicts".
- confidence is your 0..1 confidence that the extracted values are correct AND specific to one
  experiment in this paper (not a vague range).
- Do not cite anything yourself; the app attaches the paper's DOI/citation.

Extract these fields ({schema_kind}): {fields}.

Respond with ONLY this JSON object (no prose, no code fences):
{{"values": {{...the fields above, missing -> null...}}, "extraction_scope": "abstract"|"full_text",
 "confidence": 0.0, "conflicts": [], "notes": "short note on what was/ wasn't available"}}
"""


def _user_prompt(candidate) -> str:
    return (f"TITLE: {candidate.title}\n\nABSTRACT: {candidate.abstract or '(no abstract)'}\n\n"
            "Extract the structured values per the rules. Respond with ONLY the JSON object.")


# --------------------------------------------------------------------------- #
# Coercion + builders
# --------------------------------------------------------------------------- #
def _num(value):
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _text(value):
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _provenance(candidate) -> E.Provenance:
    return E.Provenance(source=candidate.source, doi=candidate.doi, title=candidate.title,
                        url=candidate.url, authors=list(candidate.authors), year=candidate.year,
                        query=candidate.query)


def _scope(value) -> str:
    v = str(value or "").strip().lower()
    return E.SCOPE_FULL_TEXT if v == "full_text" else E.SCOPE_ABSTRACT


def _empty(schema_kind, prov, *, note, status, scope=E.SCOPE_ABSTRACT):
    cls = E.LeachingEvidence if schema_kind == E.SCHEMA_LEACHING else E.CompositeEvidence
    return cls(provenance=prov, extraction_confidence=0.0, extraction_scope=scope,
               extraction_status=status, notes=note)


def _elements(value) -> dict:
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            sym = ("REE" if str(k).upper() == "REE"
                   else str(k)[:1].upper() + str(k)[1:].lower() if len(str(k)) > 1 else str(k).upper())
            if sym in E.LEACHING_ELEMENTS:
                out[sym] = _num(v)
    return out


def _build(schema_kind, payload, prov):
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    values = values or {}
    scope = _scope(payload.get("extraction_scope"))
    conf = _num(payload.get("confidence")) or 0.0
    conf = max(0.0, min(1.0, conf))
    conflicts = [str(c) for c in (payload.get("conflicts") or []) if str(c).strip()]
    note = _text(payload.get("notes"))

    common = dict(provenance=prov, extraction_confidence=conf, extraction_scope=scope,
                  extraction_status=E.STATUS_OK, conflicts=conflicts, notes=note)
    if schema_kind == E.SCHEMA_LEACHING:
        ev = E.LeachingEvidence(
            **common,
            **{f: _num(values.get(f)) for f in _NUMERIC_LEACHING},
            **{f: _text(values.get(f)) for f in _TEXT_LEACHING},
            elements_measured=[str(e) for e in (values.get("elements_measured") or []) if str(e).strip()],
            element_values_mM=_elements(values.get("element_values_mM")))
    else:
        ev = E.CompositeEvidence(
            **common,
            **{f: _num(values.get(f)) for f in _NUMERIC_COMPOSITE},
            **{f: _text(values.get(f)) for f in _TEXT_COMPOSITE})
    return ev


def _ai_extract(candidate, schema_kind, *, client, model):
    """One grounded AI call → validated payload dict, or ``None`` (never raises; no raw stored)."""
    resolved = ai_client.get_client(client, model=model)
    if not resolved.ok or resolved.client is None:
        return None
    try:
        resp = resolved.client.messages.create(
            model=ai_config.resolve_model(model), max_tokens=MAX_TOKENS,
            system=_system_prompt(schema_kind),
            messages=[{"role": "user", "content": _user_prompt(candidate)}])
    except Exception:                                       # noqa: BLE001 — never crash
        return None
    payload = _parse_json(_message_text(resp))              # the raw text is discarded here
    return payload if isinstance(payload, dict) else None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def extract_evidence(candidate, schema_kind=E.SCHEMA_LEACHING, *, client=None, model=None):
    """Extract a structured evidence row from a paper candidate (never raises, never fabricates).

    * no abstract → empty row, ``status=no_text`` (cannot extract from a title alone).
    * AI off → empty row, ``status=ai_unavailable`` (enter values manually; nothing is invented).
    * AI on → one grounded call, validated into the schema (missing → null, confidence explicit,
      conflicts flagged, abstract-scope confidence-capped). The raw model response is never stored.
    """
    prov = _provenance(candidate)
    if not candidate.has_abstract:
        return _empty(schema_kind, prov, status=E.STATUS_NO_TEXT,
                      note="No abstract/full text available — cannot extract values from a title alone.")
    if not ((client is not None) or ai_config.is_enabled()):
        return _empty(schema_kind, prov, status=E.STATUS_AI_OFF,
                      note="AI extraction is off — enable AI or enter values manually. No values were invented.")
    payload = _ai_extract(candidate, schema_kind, client=client, model=model)
    if payload is None:
        return _empty(schema_kind, prov, status=E.STATUS_AI_FAILED,
                      note="AI extraction was unavailable for this paper — no values were invented.")
    return _build(schema_kind, payload, prov)
