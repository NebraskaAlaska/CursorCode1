"""Release-gated PHREEQC integration checks against the configured real runtime.

These tests remain optional for a developer without PHREEQC.  Release/container CI sets
``PHREEQC_INTEGRATION_REQUIRED=1``; in that mode missing resources are failures, never skips.
All jobs are simulations or software smoke tests, not experimental validation.
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from flyash_phreeqc_ml.simulation import phreeqc_executor as executor
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as builder
from flyash_phreeqc_ml.simulation import phreeqc_run_contract as contract
from flyash_phreeqc_ml.simulation import source_terms


REQUIRED = os.environ.get("PHREEQC_INTEGRATION_REQUIRED") == "1"
EXPECTED_DATABASE_SHA256 = "59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10"


def _availability():
    available = executor.check_availability()
    if REQUIRED:
        assert available.can_run, available.message
        assert available.environment_identity is not None
    elif not available.can_run or available.environment_identity is None:
        pytest.skip("real PHREEQC runtime is not configured")
    return available


def _ready_preview(text: str, scenario_id: str):
    return builder.PhreeqcInputPreview(
        scenario_id=scenario_id,
        phreeqc_input_text=text,
        template_type=builder.TEMPLATE_NAOH,
        status=builder.STATUS_READY,
        includes_source_terms=True,
        source_term_mode=source_terms.MODE_GLOBAL,
        source_term_status=source_terms.STATUS_RELEASE_INCLUDED,
    )


def _execute(text: str, scenario_id: str, workdir: Path):
    available = _availability()
    preview = _ready_preview(text, scenario_id)
    reviewed = contract.review_preview(preview)
    confirmed = contract.confirm_reviewed(reviewed, available.environment_identity)
    return executor.execute_preview(preview, confirmation=confirmed, workdir=workdir)


def test_release_runtime_and_official_database_identity():
    available = _availability()
    identity = available.environment_identity
    assert identity.executable.sha256 == hashlib.sha256(
        Path(identity.executable.resolved_path).read_bytes()).hexdigest()
    assert identity.database.sha256 == hashlib.sha256(
        Path(identity.database.resolved_path).read_bytes()).hexdigest()
    if REQUIRED or os.environ.get("PHREEQC_DATABASE_ID") in {
            "usgs-phreeqc-dat-3.8.6-17100", "usgs.phreeqc.database.phreeqc"}:
        assert identity.database.sha256 == EXPECTED_DATABASE_SHA256
    configured_version = os.environ.get("PHREEQC_VERSION")
    if REQUIRED:
        assert configured_version == "3.8.6-17100"
    if os.environ.get("VLAB_RESOURCE_CATALOG"):
        provenance = available.release_provenance
        assert provenance["resource_identity_status"] == "verified_active_catalog"
        assert provenance["runtime_installation_id"] == os.environ[
            "VLAB_ACTIVE_RUNTIME_INSTALLATION_ID"]
        assert provenance["database_installation_id"] == os.environ[
            "VLAB_ACTIVE_DATABASE_INSTALLATION_ID"]
        assert provenance["resource_catalog_hash"]
        assert provenance["resource_catalog_generation"] >= 1
        assert provenance["runtime_manifest_sha256"]
        assert provenance["resource_bootstrap_result_sha256"]
        assert provenance["knowledge_pack_hash"] == os.environ["VLAB_KNOWLEDGE_PACK_HASH"]


def test_official_usgs_example_executes_as_software_smoke(tmp_path):
    available = _availability()
    example = os.environ.get("PHREEQC_OFFICIAL_EXAMPLE")
    if not example:
        if REQUIRED:
            pytest.fail("PHREEQC_OFFICIAL_EXAMPLE is required in the release image")
        pytest.skip("official example path is not configured")
    example_path = Path(example)
    assert example_path.is_file()
    result = _execute(
        example_path.read_text(encoding="utf-8", errors="strict"),
        "OFFICIAL-USGS-EXAMPLE-1",
        tmp_path / "official-example-jobs",
    )
    assert result.status == executor.STATUS_SUCCESS, result.error_message
    text = Path(result.output_path).read_text(encoding="utf-8", errors="replace")
    assert "End of Run" in text and not executor._error_lines(text)


def test_minimal_reviewed_job_selected_output_and_provenance(tmp_path):
    text = """TITLE Phase 4 software integration smoke (not experimental validation)
SOLUTION 1
    temp 25
    pH 7
    units mol/kgw
SELECTED_OUTPUT
    -file selected.out
    -reset false
    -pH true
END
"""
    result = _execute(text, "PHASE4-SMOKE", tmp_path / "jobs")
    assert result.status == executor.STATUS_SUCCESS, result.error_message
    assert result.selected_output_path and Path(result.selected_output_path).is_file()
    parsed = executor.parse_outputs(result)
    assert parsed.parse_status in {executor.PARSE_PARSED, executor.PARSE_PARTIAL}
    assert parsed.selected_output is not None and not parsed.selected_output.empty
    assert result.executable_sha256 and result.database_sha256
    assert result.environment_identity_hash
    assert result.workspace_path and Path(result.workspace_path).parent == (tmp_path / "jobs")
    if os.environ.get("VLAB_RESOURCE_CATALOG"):
        assert result.resource_identity_status == "verified_active_catalog"
        assert result.runtime_installation_id
        assert result.database_installation_id
        assert result.resource_catalog_hash
        assert result.runtime_manifest_sha256
        assert result.resource_bootstrap_result_sha256
        assert result.knowledge_pack_hash


def test_real_runtime_malformed_input_fails_closed(tmp_path):
    malformed = (
        "TITLE malformed software test\nSOLUTION 1\n    pH 7\n"
        "EQUILIBRIUM_PHASES 1\n    Definitely_Not_In_Official_Database 0 0\nEND\n"
    )
    result = _execute(malformed, "PHASE4-MALFORMED", tmp_path / "jobs")
    assert result.status == executor.STATUS_FAILED
    assert "ERROR" in (result.error_message or "")


def test_real_runtime_concurrent_jobs_are_isolated_and_cleanup_is_scoped(tmp_path):
    text = """TITLE concurrent PHREEQC software isolation smoke
SOLUTION 1
    temp 25
    pH 7
SELECTED_OUTPUT
    -file selected.out
    -reset false
    -pH true
END
"""

    def run(index: int):
        return _execute(text, f"PHASE4-CONCURRENT-{index}", tmp_path / "jobs")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))

    assert all(item.status == executor.STATUS_SUCCESS for item in results)
    assert len({item.job_id for item in results}) == 4
    assert len({item.workspace_path for item in results}) == 4
    assert all(Path(item.output_path).is_file() for item in results)
    assert all(Path(item.selected_output_path).is_file() for item in results)
    assert executor.cleanup_job_workspace(results[0]) is True
    assert not Path(results[0].workspace_path).exists()
    assert all(Path(item.workspace_path).is_dir() for item in results[1:])
