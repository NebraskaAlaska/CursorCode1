"""Make the sandbox ``backend/`` importable for these tests.

The backend is a flat set of modules (``schemas``, ``chem``, ``routes_*`` …) meant to be runnable on
its own. Putting that directory on ``sys.path`` here lets the tests ``import schemas`` etc. directly,
exactly as ``main.py`` / ``uvicorn`` do when run from the backend folder.

These tests live OUTSIDE the project's ``testpaths = ["tests"]``, so the default ``python -m pytest``
does not collect them and the main suite is unaffected. Run them explicitly::

    python -m pytest experimental/lab_sandbox_game/backend/tests
"""
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
