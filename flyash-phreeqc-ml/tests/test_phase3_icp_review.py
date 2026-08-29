"""Phase 3 durable ICP review tests; every concentration is synthetic test data."""
from __future__ import annotations

from copy import deepcopy

import pytest

from flyash_phreeqc_ml.instruments import icp_processor as icp
from flyash_phreeqc_ml.instruments import icp_review
from flyash_phreeqc_ml.workspace_store import ImmutableRecordError, WorkspaceStore


RESOLVED_AT = "2026-08-29T00:00:00+00:00"
SYNTHETIC_USER = "synthetic-test-reviewer"


def _row(row_id: str, role: str, value, *, sample_id: str = "S1", **extra) -> dict:
    return {
        "row_id": row_id,
        "sample_id": sample_id,
        "element": "Ca",
        "concentration": value,
        "unit": "mM",
        "dilution_factor": 1.0,
        "measured_or_predicted": role,
        **extra,
    }


def _context(tmp_path):
    store = WorkspaceStore(tmp_path / "synthetic-test-workspace")
    project = store.create_project("Synthetic test ICP project")
    material = store.create_material(project.project_id, "Synthetic test fly ash")
    return store, project, material


def _simple_rows() -> list[dict]:
    return [
        _row("m", "measured", 2.0),
        _row("p", "predicted", 1.25),
    ]


def _prepare(project, material, rows, **kwargs):
    return icp_review.prepare_icp_review(
        project_id=project.project_id,
        material_id=material.material_id,
        rows=rows,
        creator=SYNTHETIC_USER,
        provenance={"fixture_status": "synthetic test only"},
        **kwargs,
    )


def test_draft_round_trip_restart_reprocess_finalize_and_linked_run(tmp_path):
    store, project, material = _context(tmp_path)
    rows = [
        _row("m1", "measured", 2.0),
        _row("m2", "measured", 2.25),
        _row("p", "predicted", 1.5),
        _row("censored", "measured", 0.01, sample_id="S2", detection_limit=0.05),
        _row("hard-invalid", "measured", -1.0, sample_id="S3"),
        _row("metadata-review", "measured", 0.5, sample_id="S4", unit=None),
    ]
    prepared = _prepare(
        project, material, rows,
        source_filename="../synthetic-test-icp.csv",
        source_import_id="synthetic-test-import",
        corrections=[{
            "row_id": "metadata-review", "field": "unit", "replacement_value": "mM",
            "resolved_by": SYNTHETIC_USER, "reason": "synthetic test metadata correction",
            "resolved_at": RESOLVED_AT,
        }],
        duplicate_selections=[{
            "sample_id": "S1", "element": "Ca", "role": "measured",
            "selected_row_id": "m2", "resolved_by": SYNTHETIC_USER,
            "reason": "synthetic test duplicate choice", "resolved_at": RESOLVED_AT,
        }],
    )
    draft = icp_review.save_icp_review(store, prepared)
    assert draft.status == "draft"
    assert draft.record_type == "icp_review"
    assert draft.source_identity["source_filename"] == "synthetic-test-icp.csv"
    duplicate_set = draft.payload["duplicate_candidate_sets"][0]
    assert duplicate_set["candidate_row_ids"] == ["m1", "m2"]
    assert {item["row_id"] for item in duplicate_set["candidates"]} == {"m1", "m2"}

    # Simulate a process/app restart by constructing a fresh store instance.
    restarted = WorkspaceStore(store.root)
    loaded = icp_review.load_icp_review(
        restarted, draft.artifact_id,
        project_id=project.project_id, material_id=material.material_id,
    )
    replayed = icp_review.reprocess_icp_review(loaded)
    assert replayed.residuals[0].measured_row_id == "m2"
    assert replayed.residuals[0].residual_mM == pytest.approx(0.75)
    statuses = {row.row_id: row.qc_status for row in replayed.corrected}
    assert statuses["censored"] == icp.QC_CENSORED
    assert statuses["hard-invalid"] == icp.QC_EXCLUDED
    assert statuses["metadata-review"] == icp.QC_USABLE

    finalized = icp_review.finalize_icp_review(
        restarted, draft.artifact_id, confirmation=draft.artifact_id,
        finalized_by=SYNTHETIC_USER, reason="completed synthetic test review", rows=rows,
    )
    assert finalized.artifact.status == "finalized"
    assert finalized.run is not None
    assert finalized.artifact.related_run_id == finalized.run.run_id
    assert finalized.run.result_data["artifact_id"] == draft.artifact_id
    assert finalized.run.result_data["source_identity"] == draft.source_identity
    assert finalized.run.epistemic_type == "advisory_interpretation"

    validation = icp_review.finalized_validation_output(finalized.artifact, rows=rows)
    eligible_ids = {row["row_id"] for row in validation["eligible_rows"]}
    assert "censored" not in eligible_ids
    assert "hard-invalid" not in eligible_ids
    assert "m1" not in eligible_ids
    assert {"m2", "p", "metadata-review"} <= eligible_ids
    assert validation["eligibility_authority"].endswith("icp_processor")


def test_correction_provenance_preserves_original_and_uses_processor_authority(tmp_path):
    _, project, material = _context(tmp_path)
    unresolved = _row("review", "measured", 2.0, unit=None)
    prepared = _prepare(project, material, [unresolved], corrections=[{
        "row_id": "review", "field": "unit", "replacement_value": "mg/L",
        "resolved_by": SYNTHETIC_USER, "reason": "synthetic test unit metadata",
        "resolved_at": RESOLVED_AT,
    }])
    correction = prepared["payload"]["corrections"][0]
    processed = prepared["payload"]["processed"]["corrected"][0]
    assert correction == {
        "row_id": "review", "field": "unit", "original_value": None,
        "replacement_value": "mg/L", "resolved_by": SYNTHETIC_USER,
        "reason": "synthetic test unit metadata", "resolved_at": RESOLVED_AT,
    }
    assert processed["supplied_unit"] is None
    assert processed["input_unit"] == "mg/L"
    assert processed["resolutions"][0]["original_value"] is None
    assert prepared["provenance"]["processor_module"].endswith("icp_processor")


def test_hard_scientific_value_cannot_be_corrected_or_approved(tmp_path):
    _, project, material = _context(tmp_path)
    with pytest.raises(icp.HardInvalidResolutionError):
        _prepare(project, material, [_row("bad", "measured", -1.0)], corrections=[{
            "row_id": "bad", "field": "concentration", "replacement_value": 1.0,
            "resolved_by": SYNTHETIC_USER, "reason": "synthetic approve-anyway attempt",
            "resolved_at": RESOLVED_AT,
        }])


def test_censored_value_stays_censored_after_save_reload_and_finalize(tmp_path):
    store, project, material = _context(tmp_path)
    rows = [_row("nd", "measured", 0.01, detection_limit=0.05)]
    draft = icp_review.save_icp_review(store, _prepare(project, material, rows))
    before = icp_review.reprocess_icp_review(draft).corrected[0]
    assert before.qc_status == icp.QC_CENSORED
    assert before.value_mM is None
    final = icp_review.finalize_icp_review(
        store, draft.artifact_id, confirmation=draft.artifact_id,
        finalized_by=SYNTHETIC_USER, reason="synthetic non-detect reviewed", rows=rows,
        create_run=False,
    ).artifact
    after = icp_review.reprocess_icp_review(final).corrected[0]
    assert after.qc_status == icp.QC_CENSORED
    assert after.supplied_concentration == 0.01
    assert after.value_mM is None
    assert icp_review.finalized_validation_output(final, rows=rows)["eligible_rows"] == []


def test_three_or_more_duplicate_resolutions_remain_permanently_conflicted(tmp_path):
    rows = [_row("m1", "measured", 1.0), _row("m2", "measured", 2.0),
            _row("p", "predicted", 1.5)]
    resolutions = [{
        "sample_id": "S1", "element": "Ca", "role": "measured",
        "selected_row_id": selected, "resolved_by": SYNTHETIC_USER,
        "reason": "synthetic conflicting selection", "resolved_at": RESOLVED_AT,
    } for selected in ("m1", "m2", "m1", "m2")]

    direct = icp.process(rows, duplicate_resolutions=resolutions)
    assert direct.residuals == []
    assert all(icp.QC_DUPLICATE_MEASURED in row.qc_codes for row in direct.corrected[:2])
    assert sum("selection remains ambiguous" in item for item in direct.warnings) >= 1

    store, project, material = _context(tmp_path)
    prepared = _prepare(project, material, rows, duplicate_selections=resolutions)
    assert prepared["payload"]["processed"]["residuals"] == []
    draft = icp_review.save_icp_review(store, prepared)
    with pytest.raises(icp_review.IcpFinalizationError, match="conflicting duplicate"):
        icp_review.finalize_icp_review(
            store, draft.artifact_id, confirmation=draft.artifact_id,
            finalized_by=SYNTHETIC_USER, reason="must remain blocked", rows=rows,
        )


def test_source_hash_mismatch_blocks_reuse_and_validation(tmp_path):
    store, project, material = _context(tmp_path)
    rows = _simple_rows()
    draft = icp_review.save_icp_review(store, _prepare(project, material, rows))
    final = icp_review.finalize_icp_review(
        store, draft.artifact_id, confirmation=draft.artifact_id,
        finalized_by=SYNTHETIC_USER, reason="synthetic source reviewed", rows=rows,
        create_run=False,
    ).artifact
    changed = deepcopy(rows)
    changed[0]["concentration"] = 999.0
    with pytest.raises(icp_review.IcpSourceMismatchError, match="do not match"):
        icp_review.assert_icp_source_matches(final, rows=changed)
    with pytest.raises(icp_review.IcpSourceMismatchError):
        icp_review.finalized_validation_output(final, rows=changed)


def test_file_source_requires_exact_bytes_even_when_rows_look_the_same(tmp_path):
    store, project, material = _context(tmp_path)
    rows = _simple_rows()
    exact_bytes = b"synthetic,test,ICP\n"
    prepared = _prepare(
        project, material, rows, source_bytes=exact_bytes,
        source_filename="synthetic.csv")
    draft = icp_review.save_icp_review(store, prepared)
    final = icp_review.finalize_icp_review(
        store, draft.artifact_id, confirmation=draft.artifact_id,
        finalized_by=SYNTHETIC_USER, reason="synthetic file source reviewed",
        source_bytes=exact_bytes, rows=rows, create_run=False,
    ).artifact
    with pytest.raises(icp_review.IcpSourceMismatchError, match="source bytes are required"):
        icp_review.finalized_validation_output(final, rows=rows)
    with pytest.raises(icp_review.IcpSourceMismatchError):
        icp_review.finalized_validation_output(
            final, rows=rows, source_bytes=b"synthetic,test,changed\n")
    assert icp_review.finalized_validation_output(
        final, rows=rows, source_bytes=exact_bytes)["residuals"]


def test_validation_adapter_is_finalized_only_and_requires_explicit_confirmation(tmp_path):
    store, project, material = _context(tmp_path)
    rows = _simple_rows()
    draft = icp_review.save_icp_review(store, _prepare(project, material, rows))
    with pytest.raises(icp_review.IcpReviewStateError, match="only a finalized"):
        icp_review.finalized_validation_output(draft, rows=rows)
    with pytest.raises(icp_review.IcpFinalizationError, match="exactly match"):
        icp_review.finalize_icp_review(
            store, draft.artifact_id, confirmation="yes",
            finalized_by=SYNTHETIC_USER, reason="synthetic review", rows=rows,
        )


def test_finalized_artifact_is_immutable_and_edit_creates_linked_revision(tmp_path):
    store, project, material = _context(tmp_path)
    rows = _simple_rows()
    original = icp_review.save_icp_review(store, _prepare(project, material, rows))
    original = icp_review.finalize_icp_review(
        store, original.artifact_id, confirmation=original.artifact_id,
        finalized_by=SYNTHETIC_USER, reason="synthetic revision base", rows=rows,
        create_run=False,
    ).artifact
    with pytest.raises(ImmutableRecordError):
        store.update_artifact(original.artifact_id, reason="silent mutation attempt")

    changed_rows = deepcopy(rows)
    changed_rows[0]["concentration"] = 2.1
    revision = icp_review.save_icp_review(
        store, _prepare(project, material, changed_rows, reason="synthetic revised source"),
        artifact_id=original.artifact_id,
    )
    reloaded_original = store.get_artifact(original.artifact_id)
    assert reloaded_original.status == "finalized"
    assert reloaded_original.revision == 1
    assert revision.status == "draft"
    assert revision.revision == 2
    assert revision.previous_artifact_id == original.artifact_id
    assert revision.logical_id == original.logical_id
    assert revision.source_identity != original.source_identity


def test_project_material_scope_is_enforced_on_reload(tmp_path):
    store, project, material = _context(tmp_path)
    draft = icp_review.save_icp_review(store, _prepare(project, material, _simple_rows()))
    other = store.create_project("Other synthetic test project")
    with pytest.raises(icp_review.IcpReviewStateError, match="selected project"):
        icp_review.load_icp_review(store, draft.artifact_id, project_id=other.project_id)
    assert [item.artifact_id for item in icp_review.list_icp_reviews(
        store, project_id=project.project_id, material_id=material.material_id)] \
        == [draft.artifact_id]


def test_tampered_saved_output_or_correction_provenance_cannot_be_reused(tmp_path):
    store, project, material = _context(tmp_path)
    prepared = _prepare(project, material, _simple_rows())
    draft = icp_review.save_icp_review(store, prepared)

    tampered_payload = deepcopy(draft.payload)
    tampered_payload["processed"]["residuals"][0]["residual_mM"] = 999.0
    tampered = store.update_artifact(draft.artifact_id, payload=tampered_payload)
    with pytest.raises(icp_review.IcpReviewStateError, match="not reproducible"):
        icp_review.reprocess_icp_review(tampered)
