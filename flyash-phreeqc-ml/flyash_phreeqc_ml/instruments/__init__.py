"""Digital Lab / Virtual Instruments (Phase 1).

A "virtual instrument" here is one of four honest things — never a fake lab device:

* a **physical-simulation** engine (PHREEQC aqueous chemistry),
* a **data-processing** module over measured/predicted data (the ICP processor),
* a **signal/pattern advisory** that plans a measurement from known references (XRD), or
* an **advisory / planning** (or trained-model) helper.

This package owns the instrument **registry** + metadata **schema**, the deterministic
prompt→instrument **router**, the **ICP** data processor and **XRD** advisory module (the two
instruments with new Phase-1 behavior beyond PHREEQC), and the cross-cutting **lab modes**
(validation / uncertainty / evidence). It imports **no AI** and runs **nothing** — execution stays
on the existing confirmation-gated PHREEQC path.
"""
from __future__ import annotations

from . import (
    icp_processor,
    instrument_registry,
    instrument_router,
    instrument_schema,
    lab_modes,
    xrd_advisory,
)

__all__ = [
    "icp_processor",
    "instrument_registry",
    "instrument_router",
    "instrument_schema",
    "lab_modes",
    "xrd_advisory",
]
