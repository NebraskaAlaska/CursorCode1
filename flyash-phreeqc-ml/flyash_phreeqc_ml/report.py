"""One-click validation report — a self-contained bundle for offline review.

:func:`build_report` writes ``experiments/<run>/outputs/validation_report_<ts>/`` so
another researcher (advisor / committee) can review *how a comparison was produced*
**without the app**: a self-contained ``report.html`` (inline CSS, base64 images),
the supporting CSVs, the audit log, figure PNGs, and a ``MANIFEST.json`` of SHA-256
hashes.

Honesty is built into the template (the **Prompt-4 inclusion rules are the truth for
the wording**): the header always carries the overall validity status, and whenever it
is not ``valid`` a standing banner says the comparison is a *workflow check, not model
validation*. No template text implies PHREEQC is validated outside the ``valid`` case.

Pure stdlib + existing deps (pandas/matplotlib). HTML is string-templated; PDF is
intentionally out of scope (future work). Report folders are run outputs (gitignored).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import (__version__, attribution, audit, calculations, config, import_mapping,
               mapping_table, mass_balance, profiles, replicates, run_manager, scenarios)
from .compare import inclusion as _inc
from .ml import residual_stats
from .viz import compare_plots, measured_overview

REPORT_DIR_PREFIX = "validation_report_"
MANIFEST_FILENAME = "MANIFEST.json"
HTML_FILENAME = "report.html"

# The standing banner whenever the comparison is not validated. {status} is the
# Prompt-4 overall validity string (e.g. "preliminary", "unsafe").
NOT_VALIDATED_BANNER = ("This comparison is {status} — it is a workflow check, "
                        "not model validation.")

# needed_simulations.csv columns — chosen to feed Prompt-11's build_input (concentration
# + L/S + temperature + time + cover/CO2 code), so the export and the runner interoperate.
NEEDED_SIM_COLUMNS = [
    "condition_key", "leachant", "concentration", "time_min", "temperature_C",
    "liquid_solid_ratio", "condition_code", "CO2_condition",
    "target_outputs_needed", "reason",
]

# Overall validity is the worst comparable variable; "valid" requires *every*
# comparable variable to be valid (never overclaim from a single variable).
_WORST_ORDER = [_inc.VALIDITY_UNSAFE, _inc.VALIDITY_NEEDS_NEW,
                _inc.VALIDITY_PRELIMINARY, _inc.VALIDITY_SINGLE_SAMPLE]

# --- Element recovery (Prompt 25) ---------------------------------------------
# Provenance classification of each recovery term — surfaced in MANIFEST.json so a
# reviewer can tell, per number, whether it is measured / derived / modeled /
# literature-confirmed. (n_in is measured OR literature-confirmed per row — the row's
# starting_provenance column is authoritative; the manifest notes both.)
CLASS_MEASURED = "measured"
CLASS_DERIVED = "derived"
CLASS_MODELED = "modeled"
CLASS_LITERATURE = "literature-confirmed"

RECOVERY_TERM_CLASSIFICATION = {
    "n_in_mmol": f"{CLASS_MEASURED}|{CLASS_LITERATURE}",   # see starting_provenance
    "starting_provenance": CLASS_MEASURED,
    "starting_citation": CLASS_LITERATURE,
    "n_liquid_mmol": CLASS_MEASURED,
    "n_solid_mmol": CLASS_MEASURED,
    "gap_mmol": CLASS_DERIVED,
    "gap_sigma_mmol": CLASS_DERIVED,
    "modeled_precipitated_mmol": CLASS_MODELED,
    "by_phase": CLASS_MODELED,
    "gap_explained_mmol": CLASS_MODELED,
    "gap_unexplained_mmol": CLASS_DERIVED,
    "unexplained_fraction": CLASS_DERIVED,
}

# An element balance is only "explained" when closed or model-explained within
# uncertainty (Prompt-4/24 honesty); the other two carry the standing caution.
RECOVERY_EXPLAINED_STATUSES = {attribution.STATUS_CLOSED, attribution.STATUS_MODEL_EXPLAINED}

# Full per-term CSV (one row per element × condition) — every term + provenance + citation.
RECOVERY_CSV_COLUMNS = [
    "condition_key", "element", "n_in_mmol", "starting_provenance", "starting_citation",
    "n_liquid_mmol", "n_solid_mmol", "gap_mmol", "gap_sigma_mmol",
    "modeled_precipitated_mmol", "by_phase", "gap_explained_mmol", "gap_unexplained_mmol",
    "unexplained_fraction", "recovery_status", "closure_status",
]
# Summary table ("where knowledge is weakest"), sortable by unexplained fraction.
RECOVERY_SUMMARY_COLUMNS = [
    "condition_key", "element", "n_in_mmol", "gap_unexplained_mmol",
    "unexplained_fraction", "recovery_status", "starting_provenance",
]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _ts() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _manifest() -> pd.DataFrame:
    rp = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if rp.exists():
        try:
            return scenarios.build_scenario_manifest(pd.read_csv(rp))
        except Exception:
            pass
    return pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS)


def _residual_col(variable: str) -> str:
    if variable == "final_pH":
        return "residual_pH"
    return f"residual_{variable[:-3]}" if variable.endswith("_mM") else f"residual_{variable}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _embed_png(path: Path) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'<img alt="{html.escape(path.stem)}" src="data:image/png;base64,{b64}">'


def _df_html(df: pd.DataFrame, *, max_rows: int = 300) -> str:
    if df is None or df.empty:
        return '<p class="muted">None.</p>'
    note = ""
    if len(df) > max_rows:
        note = f'<p class="muted">Showing first {max_rows} of {len(df)} rows.</p>'
        df = df.head(max_rows)
    return note + df.to_html(index=False, border=0, classes="t", na_rep="")


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


# --------------------------------------------------------------------------- #
# Figures (written as PNG files AND embedded base64 in the HTML)
# --------------------------------------------------------------------------- #
def _overview_figure(data: pd.DataFrame, variable: str, out_path: Path,
                     profile) -> Path | None:
    """Measured-data-only overview scatter for one variable (no model comparison)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ov = measured_overview.prepare_overview(data, variable, profile)
    plot = ov.get("plot")
    if plot is None or plot.empty:
        return None
    conditions = sorted(plot["condition_key"].astype(str).unique())
    xpos = {c: i for i, c in enumerate(conditions)}
    fig, ax = plt.subplots(figsize=(7.0, 3.8))
    ax.scatter([xpos[str(c)] for c in plot["condition_key"]], plot["value"],
               color="#1f77b4", edgecolor="white", linewidth=0.5, s=42, zorder=3)
    ax.set_xticks(range(len(conditions)))
    ax.set_xticklabels(conditions, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel(variable)
    ax.set_title(f"Measured data only — {variable} (no model comparison)")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path


# --------------------------------------------------------------------------- #
# Build the supporting data tables
# --------------------------------------------------------------------------- #
def _inclusion_by_variable(data, mapping, comparison_df, manifest, profile) -> dict:
    """Run Prompt-4 inclusion for each comparable variable that has measured data."""
    out: dict[str, dict] = {}
    if comparison_df is None or comparison_df.empty:
        return out
    for var, (mcol, _p) in profile.comparison_variable_spec.items():
        if mcol not in comparison_df.columns:
            continue
        out[var] = _inc.comparison_inclusion(
            data, mapping, comparison_df, var, manifest=manifest, profile=profile)
    return out


def _overall_validity(inclusions: dict, attribution_status: str | None = None) -> str:
    """Aggregate per-variable validity (Prompt-4). 'valid' only if all are valid.

    ``attribution_status`` (Prompt-24 mass-balance closure, optional) is folded in as a
    one-source-of-truth rule: a run whose element budget is **not measured-closed**
    (the attribution is anything other than ``closed`` — i.e. ``model-explained`` /
    ``partially-explained`` / ``unexplained``) cannot be reported as ``valid``; it is
    capped at ``preliminary``. ``None`` (no mass balance) preserves the prior behaviour.
    """
    data_validities = [inc["validity"] for inc in inclusions.values()
                       if inc["validity"] != _inc.VALIDITY_NONE]
    if not data_validities:
        validity = _inc.VALIDITY_NONE
    elif all(v == _inc.VALIDITY_VALID for v in data_validities):
        validity = _inc.VALIDITY_VALID
    else:
        validity = next((v for v in _WORST_ORDER if v in data_validities),
                        _inc.VALIDITY_PRELIMINARY)
    if (validity == _inc.VALIDITY_VALID and attribution_status is not None
            and attribution_status != "closed"):
        return _inc.VALIDITY_PRELIMINARY
    return validity


def _mass_balance_attribution_status(data, profile) -> str | None:
    """Worst measured mass-balance closure status, or None when the profile opts out.

    The report has no live PHREEQC run, so it uses the *measured* status (closed vs.
    unexplained open gap) from :mod:`attribution` — enough to keep a run with an open
    element budget out of ``valid``. None when the profile declares no mass balance.
    """
    from . import attribution, mass_balance
    if not mass_balance.is_enabled(profile) or data is None or data.empty:
        return None
    results = []
    for _, r in data.iterrows():
        row = r.to_dict()
        for el in profiles.mass_balance_elements(profile):
            results.append(attribution.attribution_unavailable(row, el, profile=profile))
    return attribution.overall_attribution_status(results)


def _excluded_rows(inclusions: dict) -> pd.DataFrame:
    frames = []
    for var, inc in inclusions.items():
        ex = inc["excluded"]
        if not ex.empty:
            ex = ex.copy()
            ex.insert(0, "variable", var)
            frames.append(ex)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["variable", *_inc.EXCLUDED_COLUMNS])


def _residuals_frame(comparison_df: pd.DataFrame, profile) -> pd.DataFrame:
    if comparison_df is None or comparison_df.empty:
        return pd.DataFrame()
    cols = ["sample_id"]
    for var, (mcol, pcol) in profile.comparison_variable_spec.items():
        rcol = _residual_col(var)
        cols += [c for c in (mcol, pcol, rcol) if c in comparison_df.columns]
    cols = list(dict.fromkeys(c for c in cols if c in comparison_df.columns))
    return comparison_df[cols].copy() if cols else pd.DataFrame()


def _predictions_used(mapping, manifest, comparison_df) -> pd.DataFrame:
    used = set()
    if mapping is not None and not mapping.empty and "phreeqc_record_key" in mapping.columns:
        used = {str(k).strip() for k in mapping["phreeqc_record_key"]
                if str(k).strip() and str(k).strip().lower() != "nan"}
    if not used:
        return pd.DataFrame()
    if manifest is not None and not manifest.empty and "phreeqc_record_key" in manifest.columns:
        keep = manifest[manifest["phreeqc_record_key"].astype(str).isin(used)]
        if not keep.empty:
            return keep.reset_index(drop=True)
    # Fallback: dedup the phreeqc_* columns from the comparison frame.
    if comparison_df is not None and "phreeqc_record_key" in comparison_df.columns:
        pcols = [c for c in comparison_df.columns
                 if c.startswith("phreeqc_") or c == "phreeqc_record_key"]
        sub = comparison_df[comparison_df["phreeqc_record_key"].astype(str).isin(used)]
        return sub[pcols].drop_duplicates().reset_index(drop=True)
    return pd.DataFrame()


def _needed_simulations(data, cond_map, manifest, comparison_df, profile) -> pd.DataFrame:
    """Conditions needing a new simulation, with Prompt-11-compatible fields."""
    needed = replicates.conditions_needing_simulation(data, cond_map, manifest)
    if needed.empty:
        return pd.DataFrame(columns=NEEDED_SIM_COLUMNS)
    ann = replicates.annotate(data, profile)
    # temperature + which measured variables this condition actually has values for.
    temp_by_ck: dict[str, str] = {}
    outputs_by_ck: dict[str, list] = {}
    measured_cols = [m for (m, _p) in profile.comparison_variable_spec.values()]
    for ck, grp in ann.groupby(replicates.CONDITION_KEY_COLUMN):
        ck = str(ck)
        if "temperature_C" in grp.columns:
            t = pd.to_numeric(grp["temperature_C"], errors="coerce").dropna()
            temp_by_ck[ck] = "" if t.empty else f"{t.iloc[0]:g}"
        outs = [c for c in measured_cols if c in grp.columns
                and pd.to_numeric(grp[c], errors="coerce").notna().any()]
        outputs_by_ck[ck] = outs

    rows = []
    for _, r in needed.iterrows():
        ck = str(r["condition_key"])
        conc = r.get("NaOH_M") if str(r.get("NaOH_M", "")).strip() else r.get("acid_M", "")
        rows.append({
            "condition_key": ck,
            "leachant": r.get("leachant", ""),
            "concentration": conc,
            "time_min": r.get("time_min", ""),
            "temperature_C": temp_by_ck.get(ck, ""),
            "liquid_solid_ratio": r.get("liquid_solid_ratio", ""),
            "condition_code": r.get("condition_code", ""),
            "CO2_condition": r.get("CO2_condition", ""),
            "target_outputs_needed": ";".join(outputs_by_ck.get(ck, [])),
            "reason": r.get("reason_needed", ""),
        })
    return pd.DataFrame(rows, columns=NEEDED_SIM_COLUMNS)


# --------------------------------------------------------------------------- #
# Element recovery (Prompt 25) — integrate measured closure + attribution + literature
# --------------------------------------------------------------------------- #
def _present(value) -> bool:
    """True for a non-blank, non-NaN cell (a measured starting assay actually present)."""
    if value in (None, ""):
        return False
    try:
        return not pd.isna(value)
    except (TypeError, ValueError):  # pragma: no cover
        return True


def _pct(part, whole) -> str:
    if part is None or whole in (None, 0):
        return "—"
    return f"{(part / whole) * 100:.0f}%"


def _recovery_narrative(rec: dict) -> str:
    """The generated per-element sentence (measured assay vs literature stand-in inline)."""
    el = rec["element"]
    if rec["closure_status"] != mass_balance.STATUS_COMPLETE:
        miss = ", ".join(rec["missing_fields"]) or "inputs"
        return f"{el}: closure incomplete — missing {miss}. Recovery not computed."
    n_in, gap = rec["n_in"], rec["gap"]
    if rec["starting_provenance"] == CLASS_LITERATURE:
        link = rec["starting_citation"] or "no link"
        start = f"literature-confirmed stand-in, {link}"
    elif rec["starting_provenance"] == CLASS_MEASURED:
        start = "measured assay"
    else:
        start = "starting assay missing"
    base = (f"Of {n_in:.3g} mmol {el} initially present ({start}), "
            f"{_pct(rec['n_liquid'], n_in)} in liquid, {_pct(rec['n_solid'], n_in)} in solid; "
            f"{gap:.3g} mmol unaccounted")
    if rec["attribution_available"] and rec["by_phase"]:
        phases = ", ".join(sorted(rec["by_phase"]))
        return (base + f", of which the model attributes {rec['gap_explained']:.3g} mmol to "
                f"{phases}, leaving {rec['gap_unexplained']:.3g} mmol unexplained.")
    if rec["attribution_available"]:
        return (base + f"; the model attributes none to a candidate phase, leaving "
                f"{rec['gap_unexplained']:.3g} mmol unexplained.")
    return (base + "; model attribution unavailable (configure PHREEQC), so all "
            f"{gap:.3g} mmol remain unexplained.")


def _recovery_record(ck, el, closure, attr, starting_prov, citation) -> dict:
    n_in, gap, gap_unexpl = closure["n_in"], closure["gap"], attr.get("gap_unexplained")
    unexpl_frac = (gap_unexpl / n_in) if (n_in not in (None, 0) and gap_unexpl is not None) \
        else None
    rec = {
        "condition_key": ck, "element": el,
        "n_in": n_in, "starting_provenance": starting_prov, "starting_citation": citation,
        "n_liquid": closure["n_liquid"], "n_solid": closure["n_solid"],
        "gap": gap, "gap_sigma": closure["gap_sigma"],
        "modeled_precipitated": attr.get("modeled_precipitated_moles"),
        "by_phase": dict(attr.get("by_phase") or {}),
        "gap_explained": attr.get("gap_explained"), "gap_unexplained": gap_unexpl,
        "unexplained_fraction": unexpl_frac,
        "recovery_status": attr["status"], "closure_status": closure["status"],
        "attribution_available": attr.get("provenance") == attribution.PROVENANCE_MODEL,
        "missing_fields": closure["missing_fields"],
    }
    rec["narrative"] = _recovery_narrative(rec)
    return rec


def _recovery_records(data, profile, run_name=None, *, selected_outputs=None) -> list[dict]:
    """Per-element-per-condition recovery: starting amount, where it went, confidence.

    Integrates the measured closure (Prompt 22), PHREEQC attribution (Prompt 24; only
    available when a parsed selected output is supplied — the report has no live run, so
    by default attribution is *unavailable* and the whole gap is unexplained), and
    **confirmed** literature starting-assay stand-ins (provenance-flagged, with the
    citation). Returns ``[]`` when the profile declares no mass balance.
    """
    if not mass_balance.is_enabled(profile) or data is None or data.empty:
        return []
    from .ai import literature  # lazy: optional AI layer, keep report import light
    elements = list(profiles.mass_balance_elements(profile))
    confirmed, overrides = [], {}
    if run_name:
        try:
            confirmed = literature.confirmed_records(run_name)
            overrides = literature.confirmed_assay_overrides(confirmed, profile)
        except Exception:
            confirmed, overrides = [], {}

    ann = replicates.annotate(data, profile)
    selected_outputs = selected_outputs or {}
    records: list[dict] = []
    for ck, grp in ann.groupby(replicates.CONDITION_KEY_COLUMN):
        ck = str(ck)
        rep = grp.iloc[0].to_dict()
        # Fill ONLY confirmed literature assays into blank starting-content cells.
        new_row, badges = literature.row_with_confirmed_assays(rep, confirmed, profile)
        sel = selected_outputs.get(ck)
        for el in elements:
            closure = mass_balance.closure(new_row, el, profile=profile)
            attr = (attribution.attribute_gap(new_row, el, sel, profile=profile)
                    if sel is not None
                    else attribution.attribution_unavailable(new_row, el, profile=profile))
            scol = f"{el}_starting_content"
            if _present(rep.get(scol)):
                starting_prov, citation = CLASS_MEASURED, ""
            elif scol in badges:
                ov = overrides.get(scol)
                cite = (ov[1].get("citation") if ov else {}) or {}
                starting_prov = CLASS_LITERATURE
                citation = literature.resolvable_link(cite) or ""
            else:
                starting_prov, citation = "missing", ""
            records.append(_recovery_record(ck, el, closure, attr, starting_prov, citation))
    return records


def _fmt_by_phase(by_phase: dict) -> str:
    return "; ".join(f"{k}={v:.4g}" for k, v in sorted((by_phase or {}).items()))


def _recovery_table(records: list[dict]) -> pd.DataFrame:
    """Full per-term frame for ``element_recovery.csv`` (every term + provenance + citation)."""
    rows = []
    for r in records or []:
        rows.append({
            "condition_key": r["condition_key"], "element": r["element"],
            "n_in_mmol": r["n_in"], "starting_provenance": r["starting_provenance"],
            "starting_citation": r["starting_citation"],
            "n_liquid_mmol": r["n_liquid"], "n_solid_mmol": r["n_solid"],
            "gap_mmol": r["gap"], "gap_sigma_mmol": r["gap_sigma"],
            "modeled_precipitated_mmol": r["modeled_precipitated"],
            "by_phase": _fmt_by_phase(r["by_phase"]),
            "gap_explained_mmol": r["gap_explained"],
            "gap_unexplained_mmol": r["gap_unexplained"],
            "unexplained_fraction": r["unexplained_fraction"],
            "recovery_status": r["recovery_status"], "closure_status": r["closure_status"],
        })
    return pd.DataFrame(rows, columns=RECOVERY_CSV_COLUMNS)


def _recovery_summary(records: list[dict]) -> pd.DataFrame:
    """Cross-element summary sorted by unexplained fraction (weakest knowledge first)."""
    full = _recovery_table(records)
    if full.empty:
        return pd.DataFrame(columns=RECOVERY_SUMMARY_COLUMNS)
    summ = full[RECOVERY_SUMMARY_COLUMNS].copy()
    # NaN unexplained fractions (incomplete closures) sort last.
    summ["_sort"] = pd.to_numeric(summ["unexplained_fraction"], errors="coerce")
    summ = summ.sort_values("_sort", ascending=False, na_position="last").drop(columns="_sort")
    return summ.reset_index(drop=True)


def _mapping_traces(data, suggestion_table, manifest, profile) -> list[dict]:
    """Per-condition compact Prompt-6 trace: matched / missing / conflicting fields."""
    traces = []
    if suggestion_table is None or suggestion_table.empty:
        return traces
    for _, row in suggestion_table.iterrows():
        ck = str(row["condition_key"])
        _sample, candidates = mapping_table.condition_candidates(
            data, ck, manifest, top_n=1, profile=profile)
        best = candidates[0] if candidates else {}
        traces.append({
            "condition_key": ck,
            "mapping_status": row.get("mapping_status", ""),
            "score": row.get("score", ""),
            "confidence": row.get("confidence", ""),
            "scenario_label": row.get("scenario_label", ""),
            "matched": best.get("matched_fields", []),
            "missing": best.get("missing_metadata", []),
            "conflicting": best.get("mismatched_fields", []),
        })
    return traces


def _warnings(audit_df: pd.DataFrame, data: pd.DataFrame) -> list[str]:
    """Warnings reconstructed from the audit log (+ import warnings on the data)."""
    out: list[str] = []
    for _, r in audit_df.iterrows():
        et, p = r["event_type"], (r["payload"] or {})
        if et == audit.EVENT_SCRIPT_RUN and not p.get("ok", True):
            out.append(f"script '{p.get('script')}' exited {p.get('exit_status')}")
        elif et == audit.EVENT_VALIDATION:
            sc = p.get("severity_counts") or {}
            if sc.get("error") or sc.get("warning"):
                out.append(f"validation: {sc.get('error', 0)} error(s), "
                           f"{sc.get('warning', 0)} warning(s)")
        elif et == audit.EVENT_INCLUSION:
            for v in (p.get("variables") or []):
                if v.get("collapse_warning"):
                    out.append(f"scenario-level collapse on {v.get('variable')}")
    if data is not None and "import_warning" in data.columns:
        for msg in data["import_warning"].dropna().astype(str).unique():
            if msg.strip():
                out.append(f"import: {msg.strip()}")
    return list(dict.fromkeys(out))  # de-dup, keep order


# --------------------------------------------------------------------------- #
# HTML assembly
# --------------------------------------------------------------------------- #
_CSS = """
:root{--green:#1a8f5a;--amber:#b67611;--red:#d0402b;--ink:#1c2530;--muted:#667085;}
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
color:var(--ink);margin:0;padding:0 0 64px;line-height:1.5;background:#fff}
.wrap{max-width:1040px;margin:0 auto;padding:24px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 8px;border-bottom:1px solid #e6e9ef;padding-bottom:4px}
h3{font-size:14px;margin:16px 0 6px}.muted{color:var(--muted);font-size:13px}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;font-size:13px}
.b-valid{background:rgba(26,143,90,.14);color:var(--green)}
.b-warn{background:rgba(182,118,17,.16);color:var(--amber)}
.b-err{background:rgba(208,64,43,.14);color:var(--red)}
.banner{padding:12px 16px;border-radius:8px;margin:12px 0;font-weight:600}
.banner.warn{background:rgba(182,118,17,.12);color:var(--amber);border:1px solid rgba(182,118,17,.4)}
.banner.err{background:rgba(208,64,43,.12);color:var(--red);border:1px solid rgba(208,64,43,.4)}
.banner.ok{background:rgba(26,143,90,.10);color:var(--green);border:1px solid rgba(26,143,90,.4)}
table.t{border-collapse:collapse;width:100%;font-size:12.5px;margin:6px 0}
table.t th,table.t td{border:1px solid #e6e9ef;padding:4px 8px;text-align:left;vertical-align:top}
table.t th{background:#f7f8fa}
img{max-width:100%;height:auto;border:1px solid #e6e9ef;border-radius:6px;margin:6px 0}
.kv{font-size:13px}.kv b{display:inline-block;min-width:160px;color:var(--muted);font-weight:500}
.trace{font-size:12px;margin:4px 0;padding:6px 8px;border-left:3px solid #e6e9ef;background:#fafbfc}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:8px;margin:0 3px 0 0}
.tag-ok{background:rgba(26,143,90,.12);color:var(--green)}
.tag-miss{background:rgba(182,118,17,.14);color:var(--amber)}
.tag-bad{background:rgba(208,64,43,.12);color:var(--red)}
code{background:#f2f4f7;padding:1px 4px;border-radius:4px}
"""


def _validity_class(status: str) -> str:
    if status == _inc.VALIDITY_VALID:
        return "b-valid"
    if status in (_inc.VALIDITY_UNSAFE, _inc.VALIDITY_NEEDS_NEW):
        return "b-err"
    return "b-warn"


def _build_html(ctx: dict) -> str:
    s: list[str] = []
    status = ctx["overall_validity"]
    stale = ctx["stale"]
    s.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    s.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    s.append(f"<title>Validation report — {_esc(ctx['run_name'])}</title>")
    s.append(f"<style>{_CSS}</style></head><body><div class='wrap'>")

    # Header — always carries validity status (+ STALE marker).
    s.append(f"<h1>Validation report — {_esc(ctx['run_name'])}</h1>")
    badge = f"<span class='badge {_validity_class(status)}'>validity: {_esc(status)}</span>"
    stale_badge = (" <span class='badge b-err'>STALE</span>" if stale else
                   " <span class='badge b-valid'>current</span>")
    s.append(f"<p>{badge}{stale_badge} "
             f"<span class='muted'>· generated {_esc(ctx['generated_at'])} · "
             f"app v{_esc(ctx['app_version'])}</span></p>")

    if stale:
        s.append("<div class='banner err'>STALE — the stored comparison no longer matches "
                 "its inputs. Re-run the workflow before relying on the numbers below.<br>"
                 + "<span class='muted'>" + "; ".join(_esc(r) for r in ctx["stale_reasons"])
                 + "</span></div>")

    # Standing honesty banner whenever not validated (Prompt-4 wording is the truth).
    if status != _inc.VALIDITY_VALID:
        cls = "err" if status in (_inc.VALIDITY_UNSAFE, _inc.VALIDITY_NEEDS_NEW) else "warn"
        s.append(f"<div class='banner {cls}'>"
                 + _esc(NOT_VALIDATED_BANNER.format(status=status)) + "</div>")
    else:
        s.append("<div class='banner ok'>All comparable variables are valid "
                 "(exact mappings, sufficient rows).</div>")

    # 1) Run metadata + provenance.
    s.append("<h2>1 · Run metadata &amp; provenance</h2>")
    meta = ctx["meta"] or {}
    s.append("<div class='kv'>")
    for label, val in [
        ("Run name", ctx["run_name"]), ("Run type", ctx["run_type"]),
        ("Comparison generated", meta.get("generated_at", "—")),
        ("Provenance current", "no — STALE" if stale else "yes"),
    ]:
        s.append(f"<div><b>{_esc(label)}</b> {_esc(val)}</div>")
    s.append("</div>")
    sources = (meta.get("sources") or {})
    if sources:
        rows = [{"input": k, "sha256": (v or {}).get("sha256", ""),
                 "size_bytes": (v or {}).get("size", "")} for k, v in sources.items()]
        s.append("<h3>Input fingerprints (from comparison_meta.json)</h3>")
        s.append(_df_html(pd.DataFrame(rows)))

    # 2) Measured-data summary + overview figures.
    s.append("<h2>2 · Measured data</h2>")
    s.append("<div class='kv'>")
    for label, val in ctx["measured_summary"].items():
        s.append(f"<div><b>{_esc(label)}</b> {_esc(val)}</div>")
    s.append("</div>")
    for img in ctx["overview_images"]:
        s.append(_embed_png(img))

    # 3) Unit conversions + verify.
    s.append("<h2>3 · Unit conversions applied</h2>")
    if ctx["conversion_summary"]:
        for c in ctx["conversion_summary"]:
            mm = c["molar_mass_g_mol"]
            head = (f"<b>{_esc(c['column'])}</b> — {_esc(c['from_unit'])} → "
                    f"{_esc(c['to_unit'])} · <code>{_esc(c['conversion_id'])}</code>")
            if mm is not None:
                head += f" · M_{_esc(c['element'])} = {_esc(mm)} g/mol"
            s.append(f"<p class='kv'>{head}<br><span class='muted'>formula: "
                     f"<code>{_esc(c['formula'])}</code></span></p>")
            if c["examples"]:
                s.append(_df_html(pd.DataFrame(c["examples"])))
    else:
        s.append("<p class='muted'>No unit conversions recorded (values imported in mM, "
                 "or a legacy run without conversion provenance).</p>")
    s.append("<h3>Re-derivation check (verify_conversions)</h3>")
    s.append(_df_html(ctx["verify_conversions"]))

    # 4) Mapping table + Prompt-6 traces.
    s.append("<h2>4 · Mapping</h2>")
    s.append(_df_html(ctx["mapping_table"]))
    for t in ctx["mapping_traces"]:
        tags = []
        for f in t["matched"]:
            tags.append(f"<span class='tag tag-ok'>✓ {_esc(f)}</span>")
        for f in t["missing"]:
            tags.append(f"<span class='tag tag-miss'>? {_esc(f)}</span>")
        for f in t["conflicting"]:
            tags.append(f"<span class='tag tag-bad'>✗ {_esc(f)}</span>")
        s.append(f"<div class='trace'><b>{_esc(t['condition_key'])}</b> — "
                 f"{_esc(t['mapping_status'])} (score {_esc(t['score'])}, "
                 f"{_esc(t['confidence'])}) → <code>{_esc(t['scenario_label'])}</code><br>"
                 + ("".join(tags) or "<span class='muted'>no field detail</span>") + "</div>")

    # 5) Inclusion counts + excluded rows.
    s.append("<h2>5 · Comparison inclusion (Prompt 4)</h2>")
    s.append(_df_html(ctx["inclusion_counts"]))
    s.append("<h3>Excluded rows (one reason each)</h3>")
    s.append(_df_html(ctx["excluded_rows"]))

    # 6) Residuals + figures.
    s.append("<h2>6 · Residuals</h2>")
    s.append("<p class='muted'>Sign convention: <code>residual = measured − model "
             "predicted</code>. Positive = measured higher than the model. Near-zero "
             "residuals indicate agreement <b>only if the mapping is scientifically "
             "valid</b>.</p>")
    s.append(_df_html(ctx["residuals"], max_rows=100))
    for img in ctx["comparison_images"]:
        s.append(_embed_png(img))

    # 7) Element recovery (Prompt 25) — measured closure + attribution + literature.
    s.append("<h2>7 · Element recovery (per element, per condition)</h2>")
    rec_records = ctx["recovery_records"]
    if not rec_records:
        s.append("<p class='muted'>This dataset profile declares no batch-reaction mass "
                 "balance, so per-element recovery is not computed.</p>")
    else:
        s.append("<p class='muted'>For each element: how much was present, where it went "
                 "(liquid / solid), the unaccounted closure gap (± σ), and how much the model "
                 "attributes. A balance is called <b>explained</b> only when its status is "
                 "<code>closed</code> or <code>model-explained</code> within uncertainty; "
                 "<code>partially-explained</code> / <code>unexplained</code> are a workflow "
                 "check, not a closed balance. Starting amount is flagged "
                 "<b>measured assay</b> vs <b>literature-confirmed stand-in</b> (DOI/link "
                 "inline).</p>")
        # The same validity system (reused): an open element budget keeps a run out of valid.
        s.append(f"<p>Overall validity (incl. element budget): "
                 f"<span class='badge {_validity_class(status)}'>{_esc(status)}</span> — "
                 f"an open element budget (<code>partially-explained</code> / "
                 f"<code>unexplained</code>) caps a run at <code>preliminary</code>, never "
                 f"<code>valid</code>.</p>")
        for r in rec_records:
            st = r["recovery_status"]
            cls = ("tag-ok" if st in RECOVERY_EXPLAINED_STATUSES else
                   "tag-miss" if st == attribution.STATUS_PARTIAL else "tag-bad")
            cite = ""
            if r["starting_provenance"] == CLASS_LITERATURE and r["starting_citation"]:
                cite = (f" · <a href='{_esc(r['starting_citation'])}'>"
                        f"{_esc(r['starting_citation'])}</a>")
            s.append(f"<div class='trace'><b>{_esc(r['condition_key'])} · "
                     f"{_esc(r['element'])}</b> <span class='tag {cls}'>{_esc(st)}</span>"
                     f"<span class='tag tag-{'ok' if r['starting_provenance']==CLASS_MEASURED else 'miss'}'>"
                     f"{_esc(r['starting_provenance'])}</span>{cite}<br>"
                     f"{_esc(r['narrative'])}</div>")
        s.append("<h3>Recovery summary (sorted by unexplained fraction — weakest knowledge "
                 "first)</h3>")
        s.append(_df_html(ctx["recovery_summary"]))
        s.append("<p class='muted'>Full per-term table with provenance + citations: "
                 "<code>element_recovery.csv</code>. Each term's classification "
                 "(measured / derived / modeled / literature-confirmed) is in "
                 "<code>MANIFEST.json</code>.</p>")

    # 8) Bias (Prompt 13) — only if present.
    s.append("<h2>8 · Systematic bias (exact mappings only)</h2>")
    if ctx["bias_table"] is not None and not ctx["bias_table"].empty:
        s.append(f"<p class='muted'>{_esc(residual_stats.NON_CLAIM_LINE)}</p>")
        s.append(_df_html(ctx["bias_table"]))
    else:
        s.append("<p class='muted'>No bias estimate — there are not enough exact-mapped "
                 "pairs yet (the gate is not met).</p>")

    # 9) Validity + mapping-status summary (verbatim).
    s.append("<h2>9 · Validity</h2>")
    s.append(_df_html(ctx["mapping_status_summary"]))
    s.append("<h3>Per-variable validity (stated verbatim)</h3><ul>")
    for var, msg in ctx["validity_lines"]:
        s.append(f"<li><b>{_esc(var)}</b>: {_esc(msg)}</li>")
    s.append("</ul>")
    s.append(f"<p><b>Overall:</b> <span class='badge {_validity_class(status)}'>"
             f"{_esc(status)}</span></p>")

    # 10) Warnings.
    s.append("<h2>10 · Warnings generated</h2>")
    if ctx["warnings"]:
        s.append("<ul>" + "".join(f"<li>{_esc(w)}</li>" for w in ctx["warnings"]) + "</ul>")
    else:
        s.append("<p class='muted'>No warnings recorded in the audit log.</p>")

    # 11) Recommended next simulations.
    s.append("<h2>11 · Recommended next simulations</h2>")
    s.append("<p class='muted'>Conditions with no exact model result. The CSV "
             "<code>needed_simulations.csv</code> carries the fields the on-demand PHREEQC "
             "runner consumes, so the two interoperate.</p>")
    s.append(_df_html(ctx["needed_simulations"]))

    s.append("<h2>Files</h2>")
    s.append(_df_html(ctx["file_index"]))
    s.append("<p class='muted'>PDF export is future work. This HTML + the CSVs + "
             "MANIFEST.json (SHA-256) are the reviewable bundle.</p>")
    s.append("</div></body></html>")
    return "".join(s)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_report(run_name: str, *, profile=None) -> Path:
    """Build the self-contained validation-report folder; return its path.

    Writes ``report.html`` + the CSVs + figure PNGs + a copy of the audit log +
    ``MANIFEST.json`` under ``experiments/<run>/outputs/validation_report_<ts>/``,
    logs an audit ``export`` event, and returns the folder path.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    cfg = run_manager.load_run_config(run_name)
    out = run_manager.run_outputs_dir(run_name) / f"{REPORT_DIR_PREFIX}{_ts()}"
    out.mkdir(parents=True, exist_ok=True)

    # --- gather inputs (lab-only reads are guarded so the report never crashes) --- #
    data = run_manager.read_data_file(run_name)

    def _safe(fn, default):
        try:
            v = fn()
            return v if v is not None else default
        except run_manager.RunManagerError:
            return default

    mapping = _safe(lambda: run_manager.read_mapping(run_name),
                    pd.DataFrame(columns=run_manager.MAPPING_COLUMNS))
    cond_map = _safe(lambda: run_manager.read_condition_mapping(run_name),
                     pd.DataFrame(columns=run_manager.CONDITION_MAPPING_COLUMNS))
    manifest = _manifest()
    comp_path = _safe(lambda: run_manager.comparison_path(run_name), None)
    comparison_df = (pd.read_csv(comp_path)
                     if comp_path is not None and comp_path.exists() else pd.DataFrame())
    meta = _safe(lambda: run_manager.read_comparison_meta(run_name), None)
    is_current, stale_reasons = _safe(
        lambda: run_manager.comparison_is_current(run_name),
        (False, ["provenance unavailable for this run type"]))
    stale = not is_current

    # --- derive ---------------------------------------------------------- #
    inclusions = _inclusion_by_variable(data, mapping, comparison_df, manifest, profile)
    # Mass-balance closure status (Prompt 24) folds into validity (one source of truth).
    # Without a PHREEQC run the measured side is known: an open gap → not measured-closed.
    attribution_status = _mass_balance_attribution_status(data, profile)
    overall_validity = _overall_validity(inclusions, attribution_status=attribution_status)
    suggestion_table = mapping_table.build_suggestion_table(data, manifest, cond_map, profile)
    overall_status = replicates.overall_mapping_status(data, mapping, manifest)
    statuses = residual_stats.collect_sample_statuses(
        data, mapping, comparison_df, manifest=manifest, profile=profile)
    bias = residual_stats.bias_table(comparison_df, statuses, profile=profile) \
        if not comparison_df.empty else pd.DataFrame()
    audit_df = audit.read_audit(run_name)

    # --- figures (files + later embedded) -------------------------------- #
    overview_images: list[Path] = []
    for var in measured_overview.available_variables(data, profile)[:6]:
        img = _overview_figure(data, var, out / f"overview_{var}.png", profile)
        if img is not None:
            overview_images.append(img)
    comparison_images = compare_plots.make_comparison_plots(comparison_df, out) \
        if not comparison_df.empty else []

    # --- CSVs ------------------------------------------------------------ #
    data.to_csv(out / "measured_clean.csv", index=False)
    _predictions_used(mapping, manifest, comparison_df).to_csv(
        out / "model_predictions_used.csv", index=False)
    suggestion_table.to_csv(out / "mapping_table.csv", index=False)
    residuals = _residuals_frame(comparison_df, profile)
    residuals.to_csv(out / "residuals.csv", index=False)
    excluded = _excluded_rows(inclusions)
    excluded.to_csv(out / "excluded_rows.csv", index=False)
    needed = _needed_simulations(data, cond_map, manifest, comparison_df, profile)
    needed.to_csv(out / "needed_simulations.csv", index=False)
    if bias is not None and not bias.empty:
        bias.to_csv(out / "bias_table.csv", index=False)
    # Element recovery (Prompt 25): measured closure + attribution + literature stand-ins.
    recovery_records = _recovery_records(data, profile, run_name)
    recovery_summary = _recovery_summary(recovery_records)
    if recovery_records:
        _recovery_table(recovery_records).to_csv(out / "element_recovery.csv", index=False)

    # Copy the audit log in (the events behind everything above).
    src_log = audit.audit_log_path(run_name)
    if src_log.exists():
        shutil.copyfile(src_log, out / src_log.name)

    # --- measured summary ------------------------------------------------ #
    ann = replicates.annotate(data, profile) if not data.empty else pd.DataFrame()
    n_conditions = int(ann[replicates.CONDITION_KEY_COLUMN].nunique()) if not ann.empty else 0
    present_vars = measured_overview.available_variables(data, profile)
    measured_summary = {
        "Rows": len(data),
        "Conditions": n_conditions,
        "Replicates (rows)": len(data),
        "Variables present": ", ".join(present_vars) or "none",
    }

    # --- inclusion counts table + verbatim validity lines ---------------- #
    inc_rows, validity_lines = [], []
    for var, inc in inclusions.items():
        inc_rows.append({
            "variable": var, "rows_plotted": inc["rows_plotted"],
            "rows_excluded": inc["n_total"] - inc["rows_plotted"],
            "unique_predictions": inc["unique_predictions_used"],
            "collapse": inc["collapse_warning"], "validity": inc["validity"],
        })
        validity_lines.append((var, inc["validity_message"]))
    inclusion_counts = pd.DataFrame(inc_rows)

    status_summary = pd.DataFrame([{
        "status": k, "n": v} for k, v in overall_status["counts"].items()])

    # --- assemble HTML --------------------------------------------------- #
    ctx = {
        "run_name": run_name, "run_type": cfg.get("run_type", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": __version__,
        "meta": meta, "stale": stale, "stale_reasons": stale_reasons,
        "overall_validity": overall_validity,
        "measured_summary": measured_summary, "overview_images": overview_images,
        "conversion_summary": import_mapping.conversion_provenance_summary(data),
        "verify_conversions": calculations.verify_conversions(data),
        "mapping_table": suggestion_table,
        "mapping_traces": _mapping_traces(data, suggestion_table, manifest, profile),
        "inclusion_counts": inclusion_counts, "excluded_rows": excluded,
        "residuals": residuals, "comparison_images": comparison_images,
        "bias_table": bias,
        "recovery_records": recovery_records, "recovery_summary": recovery_summary,
        "mapping_status_summary": status_summary, "validity_lines": validity_lines,
        "warnings": _warnings(audit_df, data),
        "needed_simulations": needed,
        "file_index": pd.DataFrame(),  # filled after we list files
    }
    # File index (everything except the HTML + MANIFEST, which come last).
    listed = sorted(p for p in out.iterdir() if p.is_file())
    ctx["file_index"] = pd.DataFrame(
        [{"file": p.name, "size_bytes": p.stat().st_size} for p in listed])

    (out / HTML_FILENAME).write_text(_build_html(ctx), encoding="utf-8")

    # --- MANIFEST.json (hashes of every file except the manifest itself) -- #
    files = []
    for p in sorted(out.iterdir()):
        if p.is_file() and p.name != MANIFEST_FILENAME:
            files.append({"file": p.name, "sha256": _sha256(p), "size": p.stat().st_size})
    manifest_doc = {
        "run_name": run_name, "app_version": __version__,
        "generated_at": ctx["generated_at"], "overall_validity": overall_validity,
        "stale": stale, "files": files,
    }
    # Tag each element-recovery term measured / derived / modeled / literature-confirmed,
    # so a reviewer can trace the provenance of every number in element_recovery.csv.
    if recovery_records:
        manifest_doc["recovery_classification"] = dict(RECOVERY_TERM_CLASSIFICATION)
    (out / MANIFEST_FILENAME).write_text(json.dumps(manifest_doc, indent=2), encoding="utf-8")

    audit.log_export(run_name, kind="validation_report", file_name=out.name,
                     n_rows=len(data))
    return out
