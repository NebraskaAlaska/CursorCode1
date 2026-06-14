"""AI-assisted literature value retrieval — *quarantined and source-bound by construction*.

This optional, opt-in layer **proposes** sourced literature values (solubility
constants, typical element assays, partition behaviour) using the Anthropic API's
server-side ``web_search`` tool. It is built so a proposed value can **never** silently
enter a calculation:

Hard guarantees (all enforced in this module)
---------------------------------------------
* **No uncited number survives.** :func:`propose_literature_values` discards — *in
  code, before display* — any candidate lacking a supporting quote or any resolvable
  source (a DOI or a URL). A DOI is normalised to ``https://doi.org/<doi>``.
* **Quarantine by construction.** Candidates are written to a **separate per-run store**
  (``experiments/<run>/outputs/literature_values.jsonl``) with provenance
  ``literature-proposed`` and ``confirmed=False`` — **never** into measured data, the
  manifest, or a comparison CSV. :mod:`mass_balance` / :mod:`attribution` only ever see
  a literature value through :func:`row_with_confirmed_assays`, which injects **only**
  ``confirmed=True`` values. An unconfirmed value is therefore ignored by every
  calculation (pinned by ``tests/test_literature.py``).
* **Confirmation is deliberate and recorded.** :func:`confirm_value` flips a record to
  ``literature-confirmed``, **retains the citation permanently**, and logs an
  :func:`audit.log_event` carrying the DOI/link + title + year — so a literature value's
  influence on any downstream number is always traceable back to the exact paper.
* **Wrong-context borrowing is a double-acknowledged choice.** If the model flags a
  conditions mismatch (different material / T / ionic strength), :func:`confirm_value`
  **refuses** unless ``acknowledge_mismatch=True`` is passed (the UI's second checkbox),
  and that acknowledgement is logged too.
* **Disabled cleanly without a key.** Like :mod:`import_assist`, the feature needs
  ``ANTHROPIC_API_KEY`` + the ``anthropic`` SDK; without them :func:`propose_literature_values`
  returns ``[]`` and the rest of the app is unaffected.

The Anthropic SDK is only touched through :mod:`import_assist`'s lazy client resolver,
so importing this module never requires the SDK.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import profiles, run_manager
from . import import_assist
from .import_assist import (  # reuse the shared, tested helpers
    _clamp_confidence,
    _message_text,
    _model,
    _parse_json,
    _resolve_client,
    is_enabled,
)

__all__ = [
    "is_enabled", "Citation", "LiteratureCandidate", "propose_literature_values",
    "propose_solubility_constants", "propose_candidate_phases", "propose_starting_assay",
    "propose_partition_behavior", "normalize_doi", "doi_link", "resolvable_link",
    "has_conditions_mismatch", "literature_store_path", "save_candidates", "read_store",
    "confirmed_records", "confirm_value", "row_with_confirmed_assays",
    "confirmed_assay_badges", "ConditionsMismatchError", "LiteratureConfirmError",
    "PROVENANCE_PROPOSED", "PROVENANCE_CONFIRMED", "SYSTEM_PROMPT",
    "LITERATURE_DATA_NOTICE", "LITERATURE_CONSENT_LABEL", "MISMATCH_ACK_LABEL",
    "MAX_QUOTE_WORDS",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
PROVENANCE_PROPOSED = "literature-proposed"     # quarantined, confirmed=False
PROVENANCE_CONFIRMED = "literature-confirmed"    # user-confirmed, citation retained

LITERATURE_STORE_FILENAME = "literature_values.jsonl"

# Copyright guard: at most one short quote per source.
MAX_QUOTE_WORDS = 25
MAX_CANDIDATES = 12            # cap how many proposals we keep per query
MAX_TOKENS = 2500

# The Anthropic server-side web-search tool (the model searches; the API runs it).
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
WEB_SEARCH_MAX_USES = 5


# Per-session consent (same spirit as import_assist / assistant).
LITERATURE_DATA_NOTICE = (
    "This optional feature sends your search request — the quantity you want (e.g. a "
    "solubility constant), the material, and your experiment's conditions — to the "
    "Anthropic API, which performs a **web search** to find sourced values. Data leaves "
    "this machine for this feature only. Every result must carry a citation (DOI or URL) "
    "and a supporting quote, is **quarantined** (never enters a calculation) until you "
    "explicitly confirm it, and nothing is saved until you do."
)
LITERATURE_CONSENT_LABEL = (
    "I understand and allow sending my search request + experiment conditions to the API "
    "for a sourced web search."
)
MISMATCH_ACK_LABEL = "I understand this value is from different conditions."


# --------------------------------------------------------------------------- #
# System prompt (the guardrails — stored here, deliberate)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = f"""\
You retrieve published literature values (solubility constants, typical element assays,
partition coefficients) for a geochemistry app, using web search. The values you return
may be shown to a scientist who could borrow them — so correctness and traceability are
non-negotiable.

Absolute rules:
1. Return ONLY values you can cite from an actual search result that has a resolvable
   DOI or URL. Prefer a DOI. Every value MUST carry the exact source and a short
   supporting quote (<= {MAX_QUOTE_WORDS} words) copied verbatim from that source.
2. NEVER fabricate a constant from memory or "general knowledge". If web search does not
   surface a reliably sourced value, return an empty candidate list and set
   "note" to "no reliable sourced value found" — do NOT guess, round from memory, or
   invent a plausible number.
3. One quote per source (copyright). Keep each supporting_quote to {MAX_QUOTE_WORDS}
   words or fewer. Do not reproduce tables or long passages.
4. ALWAYS report the conditions the value was measured at (temperature, pH, ionic
   strength) as stated in the source — even when they differ from the user's experiment.
   In conditions_match, honestly assess whether those conditions match the user's
   experiment and list every mismatch (different material, temperature, ionic strength,
   pH) in mismatch_flags. A mismatch is information, not a reason to hide the value.
5. Do not silently convert units or extrapolate. Report the value in the source's own
   unit and state that unit.

Respond with ONLY a JSON object (no prose, no code fences):
{{"candidates": [
   {{"value": <number>, "unit": <string>, "quantity": <what it is, e.g. "calcite log Ksp">,
    "material": <string>, "element": <element symbol or null>, "phase": <phase name or null>,
    "conditions": {{"temperature_C": <num|null>, "pH": <num|null>, "ionic_strength": <string|null>,
                   "notes": <string|null>}},
    "citation": {{"doi": <string|null>, "url": <string|null>, "title": <string>,
                 "authors": <string|null>, "year": <int|null>,
                 "supporting_quote": <verbatim quote <= {MAX_QUOTE_WORDS} words>}},
    "conditions_match": {{"matches": <bool>, "assessment": <short string>,
                         "mismatch_flags": [<string>, ...]}},
    "confidence": <0..1>}}
 ],
 "note": <string>}}
"""


# --------------------------------------------------------------------------- #
# DOI / link helpers
# --------------------------------------------------------------------------- #
def normalize_doi(doi: str | None) -> str | None:
    """Strip any URL/``doi:`` prefix and surrounding noise → a bare DOI (or ``None``)."""
    if not doi:
        return None
    s = str(doi).strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi:", "DOI:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):]
            break
    s = s.strip().strip("/")
    return s or None


def doi_link(doi: str | None) -> str | None:
    """A resolvable ``https://doi.org/<doi>`` link, or ``None`` if no DOI."""
    bare = normalize_doi(doi)
    return f"https://doi.org/{bare}" if bare else None


def resolvable_link(citation: "Citation | dict | None") -> str | None:
    """The best resolvable link for a citation — DOI preferred, URL fallback."""
    if citation is None:
        return None
    if isinstance(citation, Citation):
        return citation.resolvable_link
    doi = (citation or {}).get("doi")
    return doi_link(doi) or ((citation or {}).get("url") or None)


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Citation:
    """A resolvable source for a literature value. ``title`` + ``supporting_quote``
    are required; at least one of ``doi`` / ``url`` must be present (enforced by the
    candidate validator, not here)."""

    title: str
    supporting_quote: str
    doi: str | None = None
    url: str | None = None
    authors: str | None = None
    year: int | None = None

    @property
    def resolvable_link(self) -> str | None:
        return doi_link(self.doi) or (self.url or None)

    def display(self) -> str:
        """A compact 'Title (Year)' label for the clickable source."""
        bits = [self.title or "untitled source"]
        if self.year:
            bits.append(f"({self.year})")
        return " ".join(bits)


@dataclass(frozen=True)
class LiteratureCandidate:
    """One proposed literature value — *always* source-bound and quarantined.

    ``confirmed`` is ``False`` until a user explicitly confirms it; only confirmed
    values may ever reach a calculation (via :func:`row_with_confirmed_assays`).
    """

    value: float
    unit: str
    quantity: str
    material: str
    conditions: dict
    citation: Citation
    conditions_match: dict
    confidence: float
    element: str | None = None
    phase: str | None = None
    kind: str = "value"
    provenance: str = PROVENANCE_PROPOSED
    confirmed: bool = False
    candidate_id: str = ""

    @property
    def source_link(self) -> str | None:
        return self.citation.resolvable_link

    @property
    def has_mismatch(self) -> bool:
        return has_conditions_mismatch(self.conditions_match)

    def to_record(self) -> dict:
        """A JSON-able store record (citation nested, ids/provenance included)."""
        rec = asdict(self)
        rec["source_link"] = self.source_link
        rec["has_mismatch"] = self.has_mismatch
        return rec


def has_conditions_mismatch(conditions_match: dict | None) -> bool:
    """True when the model flagged any conditions mismatch for a candidate."""
    cm = conditions_match or {}
    if cm.get("matches") is False:
        return True
    flags = cm.get("mismatch_flags") or []
    return bool([f for f in flags if str(f).strip()])


# --------------------------------------------------------------------------- #
# Parsing + validation (drop uncited / quote-less candidates IN CODE)
# --------------------------------------------------------------------------- #
def _truncate_quote(quote: str) -> str:
    """Enforce the copyright guard: at most :data:`MAX_QUOTE_WORDS` words."""
    words = str(quote or "").split()
    if len(words) <= MAX_QUOTE_WORDS:
        return " ".join(words)
    return " ".join(words[:MAX_QUOTE_WORDS]) + " …"


def _num_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _candidate_id(value, unit, quantity, link) -> str:
    basis = "|".join(str(x) for x in (quantity, value, unit, link))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _candidate_from_dict(d: dict, *, kind: str = "value") -> LiteratureCandidate | None:
    """Build a validated candidate, or ``None`` if it must be dropped.

    Dropped (never shown) unless it has a supporting quote AND at least one resolvable
    source (DOI or URL). A numeric value is required; a DOI is normalised to a link.
    """
    if not isinstance(d, dict):
        return None
    cite_raw = d.get("citation") if isinstance(d.get("citation"), dict) else {}
    quote = str(cite_raw.get("supporting_quote") or "").strip()
    doi = normalize_doi(cite_raw.get("doi"))
    url = cite_raw.get("url")
    url = str(url).strip() if url not in (None, "") else None

    # ---- the two hard gates: a quote AND a resolvable source ----
    if not quote:
        return None
    if not doi and not url:
        return None

    value = _num_or_none(d.get("value"))
    if value is None:
        return None
    unit = str(d.get("unit") or "").strip()
    quantity = str(d.get("quantity") or "").strip()
    if not unit or not quantity:
        return None

    citation = Citation(
        title=str(cite_raw.get("title") or "untitled source").strip(),
        supporting_quote=_truncate_quote(quote),
        doi=doi, url=url,
        authors=(str(cite_raw.get("authors")).strip() if cite_raw.get("authors") else None),
        year=_int_or_none(cite_raw.get("year")),
    )
    conditions = d.get("conditions") if isinstance(d.get("conditions"), dict) else {}
    cmatch = d.get("conditions_match") if isinstance(d.get("conditions_match"), dict) else {}
    cmatch = {
        "matches": bool(cmatch.get("matches", True)),
        "assessment": str(cmatch.get("assessment") or "").strip(),
        "mismatch_flags": [str(f).strip() for f in (cmatch.get("mismatch_flags") or [])
                           if str(f).strip()],
    }
    link = citation.resolvable_link
    return LiteratureCandidate(
        value=value, unit=unit, quantity=quantity,
        material=str(d.get("material") or "").strip(),
        conditions=conditions, citation=citation, conditions_match=cmatch,
        confidence=_clamp_confidence(d.get("confidence")),
        element=(str(d.get("element")).strip() if d.get("element") else None),
        phase=(str(d.get("phase")).strip() if d.get("phase") else None),
        kind=kind,
        candidate_id=_candidate_id(value, unit, quantity, link),
    )


# --------------------------------------------------------------------------- #
# The proposer (web search; suggestion-only)
# --------------------------------------------------------------------------- #
def _build_user_prompt(query: dict) -> str:
    """Compose the search request from a query dict (kept compact + explicit)."""
    q = dict(query or {})
    lines = ["Find published, citable literature values for this request.", ""]
    if q.get("quantity"):
        lines.append(f"Quantity wanted: {q['quantity']}")
    if q.get("material"):
        lines.append(f"Material: {q['material']}")
    if q.get("element"):
        lines.append(f"Element: {q['element']}")
    if q.get("phase"):
        lines.append(f"Phase / mineral: {q['phase']}")
    exp = q.get("experiment_conditions") or q.get("conditions")
    if exp:
        lines.append(f"User's experiment conditions (for the conditions_match "
                     f"assessment): {json.dumps(exp, ensure_ascii=False)}")
    if q.get("notes"):
        lines.append(f"Notes: {q['notes']}")
    lines.append("")
    lines.append("Return at most "
                 f"{MAX_CANDIDATES} candidates, each with a resolvable DOI or URL and a "
                 "short supporting quote. If none can be reliably sourced, return an "
                 'empty list and note "no reliable sourced value found".')
    return "\n".join(lines)


def propose_literature_values(query: dict, *, client=None, model=None,
                              kind: str | None = None) -> list[LiteratureCandidate]:
    """Propose sourced literature values for ``query`` (suggestion only, quarantined).

    ``query`` carries ``quantity`` (required for a useful search), ``material``, optional
    ``element`` / ``phase``, and ``experiment_conditions`` (used for the conditions-match
    assessment). Returns validated :class:`LiteratureCandidate` objects — **every** one
    carries a resolvable source and a supporting quote (uncited/quote-less proposals are
    dropped here, in code). Returns ``[]`` when disabled or on any API/parse failure, and
    when the model honestly reports "no reliable sourced value found".
    """
    client = _resolve_client(client)
    if client is None:
        return []
    kind = kind or str((query or {}).get("kind") or "value")

    try:
        resp = client.messages.create(
            model=_model(model),
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[{"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search",
                    "max_uses": WEB_SEARCH_MAX_USES}],
            messages=[{"role": "user", "content": _build_user_prompt(query)}],
        )
    except Exception:
        return []

    parsed = _parse_json(_message_text(resp))
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("candidates")
    if not isinstance(raw, list):
        return []

    out: list[LiteratureCandidate] = []
    seen: set[str] = set()
    for item in raw[: MAX_CANDIDATES * 2]:   # parse a few extra; dropped ones don't count
        cand = _candidate_from_dict(item, kind=kind)
        if cand is None or cand.candidate_id in seen:
            continue
        seen.add(cand.candidate_id)
        out.append(cand)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


# Thin convenience wrappers for the wired use cases (build the query; same engine).
def propose_solubility_constants(material, phase=None, *, experiment_conditions=None,
                                 client=None, model=None) -> list[LiteratureCandidate]:
    """Solubility data (e.g. log Ksp) for a material/phase."""
    return propose_literature_values({
        "kind": "solubility_constant",
        "quantity": f"{phase or material} solubility constant (log Ksp)",
        "material": material, "phase": phase,
        "experiment_conditions": experiment_conditions,
    }, client=client, model=model, kind="solubility_constant")


def propose_candidate_phases(material, *, experiment_conditions=None,
                             client=None, model=None) -> list[LiteratureCandidate]:
    """Candidate precipitate phases (+ solubility) for a novel material's EQUILIBRIUM_PHASES."""
    return propose_literature_values({
        "kind": "candidate_phase",
        "quantity": f"candidate secondary precipitate phases and their solubility "
                    f"constants for {material} leaching/alteration",
        "material": material,
        "experiment_conditions": experiment_conditions,
        "notes": "List likely secondary mineral phases that could precipitate, each with "
                 "a sourced solubility constant. Put the phase name in 'phase'.",
    }, client=client, model=model, kind="candidate_phase")


def propose_starting_assay(material, element, *, unit="wt%", experiment_conditions=None,
                           client=None, model=None) -> list[LiteratureCandidate]:
    """A typical starting element assay (stand-in when no measured assay exists)."""
    return propose_literature_values({
        "kind": "starting_assay",
        "quantity": f"typical {element} content (bulk assay) of {material}, in {unit}",
        "material": material, "element": element,
        "experiment_conditions": experiment_conditions,
        "notes": f"Report the value in {unit} if the source gives it; otherwise report "
                 "the source's own unit and state it. This is a typical/representative "
                 "assay, not a measurement of the user's sample.",
    }, client=client, model=model, kind="starting_assay")


def propose_partition_behavior(material, element, *, experiment_conditions=None,
                               client=None, model=None) -> list[LiteratureCandidate]:
    """Partition / distribution behaviour (e.g. Kd) for an element in a material."""
    return propose_literature_values({
        "kind": "partition",
        "quantity": f"partition / distribution coefficient (Kd) for {element} in {material}",
        "material": material, "element": element,
        "experiment_conditions": experiment_conditions,
    }, client=client, model=model, kind="partition")


# --------------------------------------------------------------------------- #
# Per-run quarantine store (separate from measured data + the manifest)
# --------------------------------------------------------------------------- #
def literature_store_path(run_name: str) -> Path:
    """Path to a run's quarantined literature store (under its outputs dir)."""
    return run_manager.run_outputs_dir(run_name) / LITERATURE_STORE_FILENAME


def read_store(run_name: str) -> list[dict]:
    """Read all stored literature records (tolerating malformed lines). Oldest first."""
    path = literature_store_path(run_name)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _write_store(run_name: str, records: list[dict]) -> None:
    path = literature_store_path(run_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, default=str) + "\n")


def save_candidates(run_name: str, candidates) -> list[dict]:
    """Persist proposed candidates to the quarantine store (``confirmed=False``).

    De-duplicates against existing records by ``candidate_id`` (keeping the existing
    record, so a re-proposal never clobbers a confirmation). Logs a
    ``literature_proposed`` audit event. Returns the records actually added.
    """
    existing = read_store(run_name)
    by_id = {str(r.get("candidate_id")): r for r in existing}
    added: list[dict] = []
    for cand in candidates or []:
        rec = cand.to_record() if isinstance(cand, LiteratureCandidate) else dict(cand)
        rec.setdefault("provenance", PROVENANCE_PROPOSED)
        rec.setdefault("confirmed", False)
        cid = str(rec.get("candidate_id") or "")
        if not cid or cid in by_id:
            continue
        by_id[cid] = rec
        existing.append(rec)
        added.append(rec)
    if added:
        _write_store(run_name, existing)
        _audit_proposed(run_name, added)
    return added


def confirmed_records(run_name: str) -> list[dict]:
    """Only the **confirmed** literature records (the only ones a calculation may use)."""
    return [r for r in read_store(run_name)
            if bool(r.get("confirmed")) and r.get("provenance") == PROVENANCE_CONFIRMED]


# --------------------------------------------------------------------------- #
# Confirmation (deliberate, recorded; double-ack on a conditions mismatch)
# --------------------------------------------------------------------------- #
class LiteratureConfirmError(Exception):
    """Raised when a candidate cannot be confirmed (not found / store error)."""


class ConditionsMismatchError(LiteratureConfirmError):
    """Raised when confirming a conditions-mismatched value without the second ack."""


def confirm_value(run_name: str, candidate_id: str, *,
                  acknowledge_mismatch: bool = False) -> dict:
    """Confirm one quarantined candidate → ``literature-confirmed`` (citation retained).

    Refuses (raises :class:`ConditionsMismatchError`) when the candidate carries a
    conditions mismatch and ``acknowledge_mismatch`` is not ``True`` — borrowing a value
    from the wrong context must be a deliberate, second-acknowledged choice. On success
    the record's ``confirmed`` flag and provenance are updated **in place**, the citation
    is kept, and an audit event (with the DOI/link + title + year) is logged so the
    value's downstream influence is traceable. Returns the updated record.
    """
    records = read_store(run_name)
    target = next((r for r in records if str(r.get("candidate_id")) == str(candidate_id)), None)
    if target is None:
        raise LiteratureConfirmError(f"no quarantined candidate {candidate_id!r} in run {run_name!r}.")
    if bool(target.get("confirmed")):
        return target  # idempotent

    mismatch = has_conditions_mismatch(target.get("conditions_match"))
    if mismatch and not acknowledge_mismatch:
        raise ConditionsMismatchError(
            "this value is from different conditions; confirming it requires the second "
            "acknowledgement (acknowledge_mismatch=True).")

    target["confirmed"] = True
    target["provenance"] = PROVENANCE_CONFIRMED
    target["acknowledged_mismatch"] = bool(mismatch and acknowledge_mismatch)
    _write_store(run_name, records)
    _audit_confirmed(run_name, target, mismatch=mismatch,
                     acknowledged=bool(mismatch and acknowledge_mismatch))
    return target


# --------------------------------------------------------------------------- #
# Quarantine gate into calculations (ONLY confirmed values; the single chokepoint)
# --------------------------------------------------------------------------- #
def _matches_starting_assay_unit(rec_unit: str, profile) -> bool:
    """A literature assay is injectable only when its unit matches the profile's
    declared starting-assay unit (no silent conversion into a closure)."""
    want = str(getattr(profile, "starting_content_unit", "wt%")).strip().lower().replace(" ", "")
    got = str(rec_unit or "").strip().lower().replace(" ", "")
    aliases = {"wt%": {"wt%", "%", "weight%", "wtpercent"}, "mg/kg": {"mg/kg", "mgkg", "ppm"}}
    return got == want or got in aliases.get(want, set())


def confirmed_assay_overrides(records, profile=None) -> dict:
    """``{f"{element}_starting_content": (value, record)}`` from **confirmed** records only.

    Filters, in code, to confirmed starting-assay records whose element is one the profile
    runs mass balance on and whose unit matches the profile's starting-assay unit. An
    unconfirmed record contributes **nothing** — this is the quarantine boundary.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    elements = set(getattr(profile, "mass_balance_elements", ()) or ())
    out: dict = {}
    for rec in records or []:
        if not (bool(rec.get("confirmed")) and rec.get("provenance") == PROVENANCE_CONFIRMED):
            continue
        if str(rec.get("kind")) != "starting_assay":
            continue
        el = rec.get("element")
        if el not in elements:
            continue
        if not _matches_starting_assay_unit(rec.get("unit"), profile):
            continue
        value = _num_or_none(rec.get("value"))
        if value is None:
            continue
        out[f"{el}_starting_content"] = (value, rec)
    return out


def row_with_confirmed_assays(row: dict, records, profile=None) -> tuple[dict, dict]:
    """Return ``(row, badges)`` with **confirmed** literature assays filled into blanks.

    The single chokepoint by which a literature value may reach :mod:`mass_balance` /
    :mod:`attribution`: only ``confirmed=True`` records are applied, and only into a
    ``{element}_starting_content`` cell the row leaves blank (never overwriting a measured
    value). ``badges`` maps the filled column → a provenance badge string carrying the
    source link, so any closure/recovery computed from it is shown as
    "starting assay: literature-confirmed (DOI …)", never as measured.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    new_row = dict(row or {})
    badges: dict = {}
    overrides = confirmed_assay_overrides(records, profile)
    for col, (value, rec) in overrides.items():
        cur = new_row.get(col)
        already = cur not in (None, "") and not (isinstance(cur, float) and cur != cur)
        if already:
            continue  # never overwrite a measured assay
        new_row[col] = value
        link = resolvable_link(rec.get("citation"))
        ref = link or (rec.get("citation") or {}).get("title") or "literature"
        badges[col] = f"starting assay: {PROVENANCE_CONFIRMED} ({ref})"
    return new_row, badges


def confirmed_assay_badges(run_name: str, profile=None) -> dict:
    """Badges for a run's confirmed starting-assay stand-ins (column → badge string)."""
    _row, badges = row_with_confirmed_assays({}, confirmed_records(run_name), profile)
    return badges


# --------------------------------------------------------------------------- #
# Audit (the permanent, traceable trail — DOI/link kept forever)
# --------------------------------------------------------------------------- #
def _audit_proposed(run_name: str, records: list[dict]) -> None:
    try:
        from .. import audit
        audit.log_literature_proposed(
            run_name,
            n_candidates=len(records),
            kinds=sorted({str(r.get("kind")) for r in records}),
            materials=sorted({str(r.get("material")) for r in records if r.get("material")}),
            candidate_ids=[str(r.get("candidate_id")) for r in records],
        )
    except Exception:  # logging must never break the feature
        pass


def _audit_confirmed(run_name: str, rec: dict, *, mismatch: bool, acknowledged: bool) -> None:
    try:
        from .. import audit
        cite = rec.get("citation") or {}
        audit.log_literature_confirmed(
            run_name,
            candidate_id=str(rec.get("candidate_id")),
            quantity=str(rec.get("quantity")),
            value=_num_or_none(rec.get("value")),
            unit=str(rec.get("unit")),
            element=rec.get("element"),
            kind=str(rec.get("kind")),
            citation_link=resolvable_link(cite),
            doi=normalize_doi(cite.get("doi")),
            title=str(cite.get("title") or ""),
            year=_int_or_none(cite.get("year")),
            conditions_mismatch=bool(mismatch),
            acknowledged_mismatch=bool(acknowledged),
        )
    except Exception:
        pass
