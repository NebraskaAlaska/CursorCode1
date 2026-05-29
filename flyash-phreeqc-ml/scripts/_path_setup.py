"""Make the ``flyash_phreeqc_ml`` package importable when running scripts directly.

Importing this module (``import _path_setup``) prepends the project root to
``sys.path``. This lets ``python scripts/01_parse_phreeqc.py`` work from any
directory and without a ``pip install -e .`` — handy when inspecting/running files
straight from Cursor. If the package *is* installed, this is a harmless no-op.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
