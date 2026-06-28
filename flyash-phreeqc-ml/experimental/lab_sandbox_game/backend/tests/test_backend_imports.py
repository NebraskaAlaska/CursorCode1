"""Import-safety: main.py and science_core.py import cleanly with or without optional deps installed."""
import main
import science_core


def test_main_is_import_safe():
    # Importing main must never crash, regardless of whether FastAPI is installed.
    assert isinstance(main._HAS_FASTAPI, bool)
    if main._HAS_FASTAPI:
        assert main.app is not None
    else:
        assert main.app is None  # graceful: no app, but the module imported fine


def test_science_core_reports_status_without_crashing():
    st = science_core.status()
    assert isinstance(st["has_science_core"], bool)
    assert st["executes_phreeqc"] is False        # the sandbox never runs PHREEQC


def test_science_core_delegation_is_optional():
    # When the core is absent these return None (caller falls back to its own mirror); when present
    # they return a dict. Either way: no crash, and never a fabricated/measured flag.
    peaks = science_core.xrd_expected_peaks(["Quartz"])
    assert peaks is None or peaks["measured"] is False
    icp = science_core.icp_process([])
    assert icp is None or icp["fabricated"] is False
