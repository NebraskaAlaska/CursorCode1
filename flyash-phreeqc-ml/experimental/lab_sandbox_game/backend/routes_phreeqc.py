"""PHREEQC station — build a reviewable input PREVIEW, then run ONLY behind the confirmation gate.

This scaffold mirrors the platform's PHREEQC lifecycle and its hard safety rule, and it preserves
that rule by being *more* conservative than production: **this experimental backend never executes
PHREEQC at all.** It builds a text preview and enforces the gate; actual execution is delegated to
the existing confirmation-gated engine in ``flyash_phreeqc_ml`` (see ``science_core`` / architecture
docs). The lifecycle:

    missing_inputs → ready_for_review → awaiting_confirmation → confirmed_not_executed

* :func:`preview` requires a composition + source term (release model) + leachant + database. Missing
  any of them returns ``missing_inputs`` with the list — and **no** preview. With all four present it
  returns ``ready_for_review`` with a deterministic ``preview_id`` and the preview text. It runs nothing.
* :func:`run` is the gate. Unknown id → error. ``confirm`` not true → ``awaiting_confirmation`` and
  nothing happens. ``confirm`` true → ``confirmed_not_executed``: the gate is satisfied, and the
  scaffold reports that execution would dispatch to the real engine — which it does not invoke here.

``auto_run`` is always ``False`` and ``executed`` is always ``False`` — the same invariants the app's
instrument router holds.
"""
from __future__ import annotations

import hashlib
import json

# The four inputs PHREEQC needs before even a preview is honest.
REQUIRED_INPUTS = ("composition", "source_term", "leachant", "database")

# Ephemeral in-process store (scaffold only — not persisted; replace with a real store in production).
_PREVIEW_STORE: dict[str, dict] = {}


def _preview_id(setup: dict) -> str:
    """Deterministic id from the canonicalised setup (same inputs → same id; no randomness)."""
    blob = json.dumps(setup, sort_keys=True, default=str)
    return "pqprev_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _missing(setup: dict) -> list[str]:
    return [k for k in REQUIRED_INPUTS if not setup.get(k)]


def _build_preview_text(setup: dict) -> str:
    """Render a clearly-labelled PHREEQC input PREVIEW. Echoes inputs; invents no numbers, runs nothing."""
    comp = setup.get("composition") or {}
    values = comp.get("values") if isinstance(comp, dict) else comp
    lines = [
        "# ===================================================================",
        "# PHREEQC INPUT PREVIEW — generated for REVIEW ONLY.",
        "# Not executed. Not validated. Numbers below are echoed from your inputs.",
        "# Execution stays behind the confirmation gate (and is delegated to the",
        "# existing flyash_phreeqc_ml engine, not this experimental scaffold).",
        "# ===================================================================",
        f"DATABASE {setup.get('database')}",
        "",
        "# --- Composition (as provided; basis: %s) ---" % (
            comp.get("basis") if isinstance(comp, dict) else "unspecified"),
    ]
    if isinstance(values, dict):
        for k, v in values.items():
            lines.append(f"#   {k}: {v}")
    else:
        lines.append(f"#   {values}")
    lines += [
        "",
        f"# --- Source term / release model (as provided) ---",
        f"#   {setup.get('source_term')}",
        "",
        "SOLUTION 1  Leachant",
        f"    # leachant (as provided): {setup.get('leachant')}",
        f"    temp      {setup.get('temperature_c', 25)}",
        f"    pH        {setup.get('ph', 7)} charge",
        f"    units     {setup.get('concentration_units', 'mmol/kgw')}",
        "    # (element lines would be assembled from the confirmed source term at run time)",
        "",
        "# REACTION / EQUILIBRIUM_PHASES blocks assembled at run time from the confirmed setup.",
        "END",
    ]
    return "\n".join(lines)


def preview(setup) -> dict:
    """Build a PHREEQC input preview from ``setup`` (a dict). Returns the lifecycle status + preview.

    Required keys: ``composition``, ``source_term``, ``leachant``, ``database`` (optional:
    ``temperature_c``, ``ph``, ``concentration_units``). Nothing is executed.
    """
    setup = dict(setup or {})
    missing = _missing(setup)
    if missing:
        return {
            "station": "phreeqc",
            "status": "missing_inputs",
            "missing": missing,
            "preview_id": None,
            "preview_text": None,
            "executed": False,
            "auto_run": False,
            "message": ("Cannot build a PHREEQC preview yet — missing: " + ", ".join(missing) +
                        ". Provide all four (composition, source_term, leachant, database)."),
        }

    pid = _preview_id(setup)
    text = _build_preview_text(setup)
    _PREVIEW_STORE[pid] = {"setup": setup, "status": "ready_for_review", "confirmed": False}
    return {
        "station": "phreeqc",
        "status": "ready_for_review",
        "preview_id": pid,
        "preview_text": text,
        "executed": False,
        "auto_run": False,
        "message": ("Preview built. Nothing was executed. Review it, then call /phreeqc/run with "
                    "confirm=true to proceed through the gate."),
    }


def run(preview_id=None, confirm=False, **_ignored) -> dict:
    """The confirmation gate. NEVER executes PHREEQC in this scaffold.

    * Unknown / missing ``preview_id`` → ``error`` (you must build a preview first).
    * ``confirm`` not truthy → ``awaiting_confirmation`` (nothing happens — the gate holds).
    * ``confirm`` truthy → ``confirmed_not_executed``: the gate is satisfied; execution is delegated to
      the existing confirmation-gated PHREEQC engine, which this experimental backend does not invoke.
    """
    if not preview_id or preview_id not in _PREVIEW_STORE:
        return {
            "station": "phreeqc",
            "status": "error",
            "executed": False,
            "auto_run": False,
            "message": "Unknown preview_id. Call /phreeqc/preview first and review the result.",
        }

    record = _PREVIEW_STORE[preview_id]
    if not confirm:
        record["status"] = "awaiting_confirmation"
        return {
            "station": "phreeqc",
            "status": "awaiting_confirmation",
            "preview_id": preview_id,
            "executed": False,
            "auto_run": False,
            "message": ("Explicit confirmation is required before any run. Nothing was executed. "
                        "Re-send with confirm=true to proceed."),
        }

    record["status"] = "confirmed_not_executed"
    record["confirmed"] = True
    return {
        "station": "phreeqc",
        "status": "confirmed_not_executed",
        "preview_id": preview_id,
        "executed": False,
        "auto_run": False,
        "dispatch_target": "flyash_phreeqc_ml PHREEQC executor (NOT invoked by this scaffold)",
        "message": ("Confirmation gate satisfied. In the integrated platform this dispatches to the "
                    "existing confirmation-gated PHREEQC engine. This experimental scaffold does NOT "
                    "execute PHREEQC — no simulation was run and no results were produced."),
    }


def reset_store() -> None:
    """Clear the ephemeral preview store (used by tests; not an API endpoint)."""
    _PREVIEW_STORE.clear()
