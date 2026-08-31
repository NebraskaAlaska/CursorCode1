"""Command-line entry point for the Streamlit-free scientific Resource Steward."""
from __future__ import annotations

from .resources.steward import main

if __name__ == "__main__":  # pragma: no cover - exercised through subprocess/CLI tests
    raise SystemExit(main())
