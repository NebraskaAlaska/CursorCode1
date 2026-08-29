"""Digital Lab / Virtual LAB instruments and canonical machine contract.

A "virtual instrument" here is one of four honest things — never a fake lab device:

* a **physical-simulation** engine (PHREEQC aqueous chemistry),
* a **data-processing** module over measured/predicted data (the ICP processor),
* a **signal/pattern advisory** that plans a measurement from known references (XRD), or
* an **advisory / planning** (or trained-model) helper.

``virtual_lab_machines`` owns the authoritative twelve-machine metadata/vocabulary. The historical
instrument schema/registry are compatibility adapters only. Specialized ICP/XRD/scientific modules
remain calculation authorities, and PHREEQC execution stays on its confirmation-gated path.
"""
from __future__ import annotations

from . import (
    icp_processor,
    icp_review,
    instrument_registry,
    instrument_router,
    instrument_schema,
    lab_modes,
    virtual_lab_machine_runner,
    virtual_lab_machines,
    xrd_advisory,
)

__all__ = [
    "icp_processor",
    "icp_review",
    "instrument_registry",
    "instrument_router",
    "instrument_schema",
    "lab_modes",
    "virtual_lab_machine_runner",
    "virtual_lab_machines",
    "xrd_advisory",
]
