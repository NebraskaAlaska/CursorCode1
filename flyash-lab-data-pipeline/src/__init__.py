"""Fly Ash Lab Data Pipeline — source package.

Modules:
    config            Column schema, default (editable) assumptions, thresholds, scoring presets.
    data_loader       Load CSV/Excel, normalise columns, build blank templates.
    calculations      Derived metrics and per-mix/age strength statistics.
    validation        Data-quality rules producing warnings/errors.
    scoring           Leaching risk and per-application reuse ranking.
    plotting          Plotly figure builders.
    report_generator  Assemble an HTML research report.
"""

__version__ = "0.1.0"
