"""Experiment-planning and QA/QC tools for Monday's lab work.

These modules are *pre-data* helpers — they design the experiment matrix, validate
a filled experimental-release CSV before it enters Phase 2, and compute simple
sustainability/cost *proxy* indicators. None of them train a model or change the
chemistry; they exist to make the measured data clean and well-described.
"""

from .plan_generator import (
    build_experiment_plan,
    make_sample_id,
    write_experiment_plan,
)
from .validate_experimental_data import (
    validate_experimental_df,
    validate_experimental_dir,
    VALIDATION_REPORT_COLUMNS,
)
from .sustainability_score import (
    SUSTAINABILITY_COLUMNS,
    compute_sustainability_scores,
)

__all__ = [
    # plan generator
    "make_sample_id",
    "build_experiment_plan",
    "write_experiment_plan",
    # validator
    "validate_experimental_df",
    "validate_experimental_dir",
    "VALIDATION_REPORT_COLUMNS",
    # sustainability
    "compute_sustainability_scores",
    "SUSTAINABILITY_COLUMNS",
]
