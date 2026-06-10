"""One consolidated measured-condition → model-prediction suggestion table.

This bridges the two existing layers without changing either:

* :mod:`replicates` groups measured rows into *conditions* (``condition_key``) so
  replicates are mapped together, and classifies a mapping into one of four
  statuses (``exact`` / ``scenario-level only`` / ``unsafe`` / ``needs new
  simulation``);
* :mod:`scenarios` scores each model (PHREEQC) scenario for a sample with
  transparent, hand-written rules (no ML) and returns the top candidates.

:func:`build_suggestion_table` produces **one row per condition** — the best
candidate scenario, its status/score/confidence, a short reason, and an
``already_mapped`` flag — so the Match tab can show suggestions automatically (no
button) and drive the accept actions and the "needs new simulation" list from a
single source of truth. It is pure (operates on the frames handed in) and does no
chemistry or scoring of its own; it only *consolidates* what the two modules
already compute.
"""
from __future__ import annotations

import pandas as pd

from . import replicates, scenarios

# One row per measured condition.
SUGGESTION_TABLE_COLUMNS = [
    "condition_key",
    "n_replicates",
    "scenario_label",
    "phreeqc_record_key",
    "mapping_status",
    "score",
    "confidence",
    "reason",
    "already_mapped",
]

# Statuses a user may accept straight from the table. ``unsafe`` is deliberately
# excluded — it must go through the Advanced manual-override path (which records
# ``override=true``); ``needs new simulation`` has no candidate to accept.
BULK_ACCEPT_STATUS = replicates.MAPPING_STATUS_EXACT
SELECTABLE_STATUSES = {
    replicates.MAPPING_STATUS_EXACT,
    replicates.MAPPING_STATUS_SCENARIO,
}


def condition_representative_sample(data: pd.DataFrame, condition_key: str,
                                    profile=None) -> dict:
    """First measured row belonging to ``condition_key`` (its metadata is shared)."""
    ann = replicates.annotate(data, profile)
    if ann.empty or replicates.CONDITION_KEY_COLUMN not in ann.columns:
        return {}
    sub = ann[ann[replicates.CONDITION_KEY_COLUMN].astype(str) == str(condition_key)]
    return sub.iloc[0].to_dict() if not sub.empty else {}


def _manifest_row(manifest: pd.DataFrame, record_key: str | None) -> dict | None:
    if not record_key or manifest is None or manifest.empty \
            or "phreeqc_record_key" not in manifest.columns:
        return None
    hit = manifest[manifest["phreeqc_record_key"].astype(str) == str(record_key)]
    return hit.iloc[0].to_dict() if not hit.empty else None


def build_suggestion_table(data: pd.DataFrame, manifest: pd.DataFrame,
                           existing_mapping=None, profile=None) -> pd.DataFrame:
    """One best-candidate suggestion row per measured condition (pure).

    Groups measured rows by :func:`replicates.condition_key` (so the mapping stays
    condition-level with replicate inheritance, matching
    :func:`replicates.expand_condition_mapping`), scores each condition's
    representative row against ``manifest`` with :func:`scenarios.suggest_mappings`,
    and classifies the best candidate with :func:`replicates.mapping_status`.

    ``existing_mapping`` is the run's condition→PHREEQC map (or any frame/dict the
    replicate helpers understand); a condition already in it gets
    ``already_mapped=True``. A condition with no usable candidate is reported with
    status ``needs new simulation`` and a blank record_key. ``profile`` (a
    :class:`profiles.DatasetProfile`) selects the grouping + condition vocab; it
    defaults to the fly-ash profile so existing callers are unchanged.
    """
    summary = replicates.replicate_summary(data, profile)
    if summary.empty:
        return pd.DataFrame(columns=SUGGESTION_TABLE_COLUMNS)

    mapped = replicates._mapping_dict(existing_mapping)
    have_manifest = manifest is not None and not manifest.empty

    rows: list[dict] = []
    for _, srow in summary.iterrows():
        ck = str(srow[replicates.CONDITION_KEY_COLUMN])
        n_rep = int(srow["number_of_replicates"])
        sample = condition_representative_sample(data, ck, profile)
        sugs = (scenarios.suggest_mappings(sample, manifest, top_n=1, profile=profile)
                if have_manifest else [])

        if not sugs:
            rows.append({
                "condition_key": ck,
                "n_replicates": n_rep,
                "scenario_label": "",
                "phreeqc_record_key": "",
                "mapping_status": replicates.MAPPING_STATUS_NEEDS_NEW,
                "score": float("nan"),
                "confidence": "low",
                "reason": "No model/simulation result exists for this condition.",
                "already_mapped": ck in mapped,
            })
            continue

        best = sugs[0]
        scenario = _manifest_row(manifest, best["suggested_phreeqc_record_key"])
        status = replicates.mapping_status(sample, scenario, profile)
        rows.append({
            "condition_key": ck,
            "n_replicates": n_rep,
            "scenario_label": best.get("scenario_label", ""),
            "phreeqc_record_key": best.get("suggested_phreeqc_record_key", ""),
            "mapping_status": status,
            "score": best.get("score"),
            "confidence": best.get("confidence", "low"),
            "reason": best.get("reason", ""),
            "already_mapped": ck in mapped,
        })

    return pd.DataFrame(rows, columns=SUGGESTION_TABLE_COLUMNS)


def exact_suggestions(table: pd.DataFrame) -> pd.DataFrame:
    """Rows eligible for bulk *Accept all exact* — status exact + a candidate row."""
    if table is None or table.empty:
        return pd.DataFrame(columns=SUGGESTION_TABLE_COLUMNS)
    keys = table["phreeqc_record_key"].astype(str).str.strip()
    return table[(table["mapping_status"] == BULK_ACCEPT_STATUS) & (keys != "")]


def needs_new_simulation(table: pd.DataFrame) -> pd.DataFrame:
    """Conditions whose best candidate is ``needs new simulation`` (same source)."""
    if table is None or table.empty:
        return pd.DataFrame(columns=SUGGESTION_TABLE_COLUMNS)
    return table[table["mapping_status"] == replicates.MAPPING_STATUS_NEEDS_NEW]


def condition_candidates(data: pd.DataFrame, condition_key: str,
                         manifest: pd.DataFrame, top_n: int = 3,
                         profile=None) -> tuple[dict, list[dict]]:
    """The representative sample + top-N scored candidates for one condition.

    Used by the row-level detail view: each candidate dict is the full
    :func:`scenarios.score_scenario` output (matched / mismatched / missing /
    ``score_breakdown`` / ``metadata_notes``) plus its record_key and label.
    """
    sample = condition_representative_sample(data, condition_key, profile)
    if manifest is None or manifest.empty:
        return sample, []
    return sample, scenarios.suggest_mappings(sample, manifest, top_n=top_n, profile=profile)
