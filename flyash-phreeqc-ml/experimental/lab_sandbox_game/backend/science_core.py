"""Optional bridge to the main platform's audited science modules (the integration seam).

The sandbox routes are deliberately **self-contained** so the backend is hermetic, testable on its
own, and liftable into a separate service. But in the integrated platform the *single source of truth*
for the science is ``flyash_phreeqc_ml`` — its ``instruments.xrd_advisory`` reference peaks, its
``instruments.icp_processor`` data reduction, and (critically) its confirmation-gated PHREEQC engine.

This module is the documented seam where that delegation would happen. It tries to import the real
package and exposes :data:`HAS_SCIENCE_CORE`. It imports nothing at module load that could fail hard,
and it **never** lets the sandbox execute PHREEQC: there is intentionally no ``run`` passthrough here.
The import is read-only and does not affect the Streamlit app's behaviour in any way.

Wiring guidance (left to integration, on purpose):

* XRD — delegate ``routes_xrd.expected`` to ``xrd_advisory.expected_peaks`` so peaks have one home.
* ICP — delegate ``routes_icp.process`` to ``icp_processor.process`` (richer units + QC).
* PHREEQC — keep building the preview here; dispatch the *confirmed* run to the app's executor, which
  owns the confirmation gate. The sandbox must not run PHREEQC itself.
"""
from __future__ import annotations

HAS_SCIENCE_CORE = False
_IMPORT_ERROR = None

try:  # pragma: no cover - depends on whether the package is importable in this process
    from flyash_phreeqc_ml.instruments import xrd_advisory as _xrd_advisory
    from flyash_phreeqc_ml.instruments import icp_processor as _icp_processor
    HAS_SCIENCE_CORE = True
except Exception as exc:  # ImportError or anything else — degrade gracefully, never crash the API
    _xrd_advisory = None
    _icp_processor = None
    _IMPORT_ERROR = repr(exc)


def status() -> dict:
    """Report whether the audited science core is available (for /health and diagnostics)."""
    return {
        "has_science_core": HAS_SCIENCE_CORE,
        "import_error": _IMPORT_ERROR,
        "executes_phreeqc": False,  # never — execution stays in the app's gated engine
        "note": ("When available, XRD/ICP can delegate to flyash_phreeqc_ml as the single source of "
                 "truth. The sandbox still never runs PHREEQC."),
    }


def xrd_expected_peaks(phases):
    """Delegate to the app's XRD advisory if present, else ``None`` (caller uses its own mirror)."""
    if not HAS_SCIENCE_CORE or _xrd_advisory is None:
        return None
    advisory = _xrd_advisory.expected_peaks(phases)
    return {"checklist": advisory.checklist_table(), "peaks": advisory.peak_table(),
            "warnings": advisory.warnings, "disclaimer": advisory.disclaimer, "measured": False}


def icp_process(rows, apply_blank: bool = True):
    """Delegate to the app's ICP processor if present, else ``None`` (caller uses its own mini-processor)."""
    if not HAS_SCIENCE_CORE or _icp_processor is None:
        return None
    result = _icp_processor.process(rows, apply_blank=apply_blank)
    return {"corrected": result.corrected_table(), "residuals": result.residual_table(),
            "warnings": result.warnings, "explanation": result.explanation, "fabricated": False}
