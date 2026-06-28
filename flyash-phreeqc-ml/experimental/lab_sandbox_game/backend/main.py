"""FastAPI binding for the sandbox backend — OPTIONAL and import-safe.

All science/contract logic lives in the ``routes_*.py`` modules as plain functions (which the tests
exercise directly, with no web framework). This module only exposes those functions over HTTP, and
only if FastAPI happens to be installed. If it is not, importing this module still succeeds: ``app``
is ``None`` and :func:`create_app` raises a clear, actionable error. That keeps ``compileall`` and the
test suite green without adding a dependency to the main platform.

Run (after ``pip install -r requirements.txt``), from this ``backend/`` directory::

    uvicorn main:app --reload --port 8000

Endpoints (see ``../api_contract.md`` for full request/response shapes)::

    GET  /health
    POST /materials/synthesize
    POST /xrd/expected
    POST /phreeqc/preview
    POST /phreeqc/run
    POST /icp/process
"""
from __future__ import annotations

import routes_icp
import routes_materials
import routes_phreeqc
import routes_xrd
import schemas
import science_core

try:  # FastAPI is optional; the scaffold and its tests do not require it.
    from fastapi import FastAPI
    _HAS_FASTAPI = True
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on the environment
    FastAPI = None  # type: ignore
    _HAS_FASTAPI = False
    _IMPORT_ERROR = repr(exc)

API_TITLE = "2D Lab Sandbox — experimental backend (scaffold)"
API_VERSION = "0.0.1-scaffold"


def create_app():
    """Build the FastAPI app, wiring each endpoint to its plain route function. Requires FastAPI."""
    if not _HAS_FASTAPI:
        raise RuntimeError(
            "FastAPI is not installed, so the HTTP layer cannot start. Install it with "
            "`pip install -r requirements.txt`. (The route logic in routes_*.py runs without "
            f"FastAPI and is what the tests use.) Import error: {_IMPORT_ERROR}")

    app = FastAPI(title=API_TITLE, version=API_VERSION)

    @app.get("/health")
    def health():  # noqa: ANN202 - scaffold
        return {"status": "ok", "scaffold": True, "executes_phreeqc": False,
                "science_core": science_core.status(), "stations": list(schemas.STATION_IDS)}

    @app.post("/materials/synthesize")
    def synthesize(payload: dict):  # noqa: ANN202
        return routes_materials.synthesize(
            name=payload.get("name"), formula=payload.get("formula"),
            composition=payload.get("composition"))

    @app.post("/xrd/expected")
    def xrd_expected(payload: dict):  # noqa: ANN202
        # Accept either a full card ({"card": {...}}) or a phase list ({"phases": [...]}).
        return routes_xrd.expected(payload.get("card") or payload.get("phases") or payload)

    @app.post("/phreeqc/preview")
    def phreeqc_preview(payload: dict):  # noqa: ANN202
        return routes_phreeqc.preview(payload.get("setup") or payload)

    @app.post("/phreeqc/run")
    def phreeqc_run(payload: dict):  # noqa: ANN202
        return routes_phreeqc.run(preview_id=payload.get("preview_id"),
                                  confirm=bool(payload.get("confirm", False)))

    @app.post("/icp/process")
    def icp_process(payload: dict):  # noqa: ANN202
        if payload.get("from_composition") is not None:  # explicit fabrication attempt → refuse
            return routes_icp.refuse_measured_from_composition()
        return routes_icp.process(payload.get("rows") or [],
                                  apply_blank=bool(payload.get("apply_blank", True)))

    return app


# Import-safe module attribute: a real app when FastAPI is present, else None.
app = create_app() if _HAS_FASTAPI else None
