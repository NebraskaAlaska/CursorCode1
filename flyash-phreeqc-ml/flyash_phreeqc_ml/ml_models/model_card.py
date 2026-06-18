"""Model **card** — the honest, exportable description of a trained model (no scikit-learn).

Every trained model carries a card (intended use, NOT-intended use, data sources, rows, features,
metrics, limitations, known failure cases, applicability domain, validation status, date, and the
standing extraction-uncertainty warning). The card is saved as JSON beside the model artifact and
can be exported as Markdown for a report or a committee review.
"""
from __future__ import annotations

from . import feature_schema, model_schema

EXTRACTION_UNCERTAINTY_WARNING = (
    "Training data extracted from literature with AI may contain extraction uncertainty "
    "(mis-read values, units, or conditions). Literature rows are reviewed and confidence-scored, "
    "but they are not a substitute for measured laboratory data.")

INTENDED_USE = (
    "Fast, experimental *surrogate* estimate of a composite mechanical property from mix / curing "
    "inputs, to triage and prioritise experiments. It is a screening tool, not a design value.")

NOT_INTENDED_USE = (
    "Not a validated design or safety value; not a measurement; not a structural-engineering "
    "input. Do not use it to certify a material, replace standardised testing, or make a load-"
    "bearing decision. Predictions outside the training range are unreliable.")


def known_failure_cases(target: str) -> list:
    return [
        "Inputs far outside the training range (extrapolation) — the estimate is unreliable.",
        "Plastic types / forms or curing regimes not represented in the training rows.",
        f"Very small training sets — the {model_schema.target_label(target)} estimate may be "
        "dominated by a few rows.",
        "Datasets built largely from abstract-only literature extractions (limited detail).",
    ]


def limitations(validation_status: str, n_train: int) -> list:
    items = [
        "Surrogate / data-driven model — it learns correlations in the training rows, not "
        "mechanism. It does not validate against independently measured experiments.",
        f"Trained on {n_train} row(s); small datasets give wide, uncertain estimates.",
        "Cross-validation metrics describe the training distribution, not field performance.",
    ]
    if validation_status == model_schema.VALIDATION_DEMO:
        items.insert(0, "DEMO MODEL — trained on SYNTHETIC data for workflow testing only. "
                        "Its numbers are meaningless; never present them as real or validated.")
    return items


def applicability_domain(feature_ranges: dict, categories_seen: dict) -> dict:
    """The validity box: numeric ranges + categorical values the model actually saw."""
    return {
        "numeric_ranges": {k: list(v) for k, v in (feature_ranges or {}).items()},
        "categorical_values": {k: list(v) for k, v in (categories_seen or {}).items()},
        "note": "Predictions are only meaningful for inputs inside these ranges / categories.",
    }


def build_model_card(*, model_type, target, source_type, validation_status, n_train, n_validation,
                     numeric_features, categorical_features, metrics, feature_ranges,
                     categories_seen, training_provenance, date, version) -> dict:
    """Assemble the model card dict (see module docstring for the contract)."""
    features = list(numeric_features) + list(categorical_features)
    return {
        "model_type": model_type,
        "model_type_label": model_schema.MODEL_TYPE_LABELS.get(model_type, model_type),
        "model_family": model_schema.MODEL_FAMILY_COMPOSITE,
        "target_output": target,
        "target_label": model_schema.target_display(target),
        "intended_use": INTENDED_USE,
        "not_intended_use": NOT_INTENDED_USE,
        "training_data_sources": dict(training_provenance or {}),
        "source_type": source_type,
        "n_rows": int(n_train),
        "n_validation_rows": int(n_validation),
        "feature_list": features,
        "feature_labels": {f: feature_schema.feature_label(f) for f in features},
        "metrics": dict(metrics or {}),
        "limitations": limitations(validation_status, n_train),
        "known_failure_cases": known_failure_cases(target),
        "applicability_domain": applicability_domain(feature_ranges, categories_seen),
        "validation_status": validation_status,
        "validation_status_label": model_schema.VALIDATION_LABELS.get(validation_status,
                                                                      validation_status),
        "date_trained": date,
        "version": version,
        "extraction_uncertainty_warning": EXTRACTION_UNCERTAINTY_WARNING,
    }


def render_markdown(card: dict) -> str:
    """Render a model card dict as Markdown (for export / a report)."""
    c = dict(card or {})
    lines = [f"# Model card — {c.get('target_label', c.get('target_output', '?'))}", ""]
    lines.append(f"- **Model type:** {c.get('model_type_label', c.get('model_type'))}")
    lines.append(f"- **Model family:** {c.get('model_family')}")
    lines.append(f"- **Validation status:** {c.get('validation_status_label', c.get('validation_status'))}")
    lines.append(f"- **Date trained:** {c.get('date_trained')}  ·  **Version:** {c.get('version')}")
    lines.append(f"- **Training rows:** {c.get('n_rows')}  "
                 f"(validation rows: {c.get('n_validation_rows')})")
    lines.append(f"- **Source type:** {c.get('source_type')}")
    lines += ["", "## Intended use", c.get("intended_use", ""), "",
              "## NOT intended use", c.get("not_intended_use", ""), ""]

    metrics = c.get("metrics") or {}
    if metrics:
        lines.append("## Metrics")
        for k, v in metrics.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    sources = c.get("training_data_sources") or {}
    if sources:
        lines.append("## Training data sources")
        for k, v in sources.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    lines.append("## Features")
    lines.append(", ".join(c.get("feature_list") or []) or "—")
    lines.append("")

    ad = c.get("applicability_domain") or {}
    lines.append("## Applicability domain")
    for k, v in (ad.get("numeric_ranges") or {}).items():
        lines.append(f"- {k}: {v[0]} … {v[1]}")
    for k, v in (ad.get("categorical_values") or {}).items():
        lines.append(f"- {k}: {', '.join(map(str, v))}")
    lines.append(ad.get("note", ""))
    lines.append("")

    lines.append("## Limitations")
    for item in c.get("limitations") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Known failure cases")
    for item in c.get("known_failure_cases") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Data caveat")
    lines.append(c.get("extraction_uncertainty_warning", ""))
    lines.append("")
    return "\n".join(lines)
