"""Five synthetic-only Phase 3 user journeys across durable restart boundaries."""
from __future__ import annotations

import json
import re

from flyash_phreeqc_ml import phase3_artifacts, workspace_store
from flyash_phreeqc_ml.experiments import plan_generator, sustainability_score
from flyash_phreeqc_ml.instruments import icp_processor, icp_review
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.instruments import xrd_advisory, xrd_records
from flyash_phreeqc_ml.literature import evidence_review
from flyash_phreeqc_ml.literature import evidence_schema as E


ACTOR = "Synthetic test researcher"
WHEN = "2026-08-29T00:00:00Z"


def _context(tmp_path, name="Synthetic test Phase 3"):
    store = workspace_store.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project(name)
    material = store.create_material(
        project.project_id, "Synthetic test material", material_type="test fixture",
        composition_provenance={"source_type": "synthetic_demo"})
    return store, project, material


def _icp_row(row_id, sample, role, value, **extra):
    return {
        "row_id": row_id, "sample_id": sample, "element": "Ca",
        "concentration": value, "unit": "mM", "dilution_factor": 1.0,
        "measured_or_predicted": role, **extra,
    }


def _evidence_payload(title="Synthetic test evidence"):
    return {
        "schema_kind": E.SCHEMA_LEACHING,
        "provenance": {
            "source": "synthetic manual fixture", "title": title,
            "authors": ["Synthetic Author"], "year": 2026,
            "url": "https://example.invalid/synthetic",
            "discovery_route": {"method": "manual", "source": "synthetic manual fixture",
                                "executed_queries": [], "record_identifier": "SYN-EV-1"},
        },
        "source_location": {
            "page": "7", "table": "Table S1", "figure": "Figure S2",
            "section": "Synthetic methods", "note": "Synthetic test location",
        },
        "topic": "synthetic factor", "claim": "synthetic reported factor",
        "reported_values": {"factor": 3.0}, "units": {"factor": "kg CO2e/kg"},
        "conditions": {"boundary": "Synthetic gate"},
        "extraction_scope": E.SCOPE_MANUAL,
        "extraction_status": E.STATUS_MANUAL,
        "extraction_confidence": 0.8,
        "field_confidence": {"factor": 0.9},
        "conflicts": [], "notes": "Structured synthetic note only",
    }


def test_journey_1_durable_icp_review_finalization_and_source_mismatch(tmp_path):
    store, project, material = _context(tmp_path, "Synthetic test ICP journey")
    rows = [
        _icp_row("m1", "S1", "measured", 2.0),
        _icp_row("m2", "S1", "measured", 2.2),
        _icp_row("p", "S1", "predicted", 1.5),
        _icp_row("nd", "S2", "measured", 0.01, detection_limit=0.05),
        _icp_row("excluded", "S3", "measured", -1.0),
        _icp_row("review", "S4", "measured", 0.4, unit=None),
    ]
    prepared = icp_review.prepare_icp_review(
        project_id=project.project_id, material_id=material.material_id,
        rows=rows, creator=ACTOR,
        corrections=[{
            "row_id": "review", "field": "unit", "replacement_value": "mM",
            "resolved_by": ACTOR, "reason": "Synthetic test metadata correction",
            "resolved_at": WHEN,
        }],
        duplicate_selections=[{
            "sample_id": "S1", "element": "Ca", "role": "measured",
            "selected_row_id": "m2", "resolved_by": ACTOR,
            "reason": "Synthetic test duplicate resolution", "resolved_at": WHEN,
        }],
        provenance={"fixture": "Synthetic test data"},
    )
    draft = icp_review.save_icp_review(store, prepared)
    store.link_artifact_to_material(draft.artifact_id)
    restarted = workspace_store.WorkspaceStore(store.root)
    loaded = icp_review.load_icp_review(
        restarted, draft.artifact_id,
        project_id=project.project_id, material_id=material.material_id)
    assert icp_review.reprocess_icp_review(loaded).residuals[0].measured_row_id == "m2"
    finalized = icp_review.finalize_icp_review(
        restarted, draft.artifact_id, confirmation=draft.artifact_id,
        finalized_by=ACTOR, reason="Synthetic test review complete", rows=rows)
    reopened_run = restarted.get_run(finalized.run.run_id)
    assert reopened_run.result_data["artifact_id"] == draft.artifact_id
    gated = icp_review.finalized_validation_output(finalized.artifact, rows=rows)
    eligible = {row["row_id"] for row in gated["eligible_rows"]}
    assert "m2" in eligible and "review" in eligible
    assert not {"m1", "nd", "excluded"} & eligible
    statuses = {row.row_id: row.qc_status for row in finalized.result.corrected}
    assert statuses["nd"] == icp_processor.QC_CENSORED
    assert statuses["excluded"] == icp_processor.QC_EXCLUDED
    changed = [dict(row) for row in rows]
    changed[0]["concentration"] = 999.0
    try:
        icp_review.finalized_validation_output(finalized.artifact, rows=changed)
    except icp_review.IcpSourceMismatchError:
        pass
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("changed ICP source was silently reused")


def _xrd_reference(raw, *, phase, record_id):
    return xrd_advisory.import_reference_csv(
        raw, source_filename=f"synthetic-{record_id}.csv",
        source_metadata={
            "source_name": "Synthetic test reference source",
            "source_record_id": record_id, "phase_name": phase,
            "formula": "AB2", "polymorph": "synthetic form",
            "radiation_source": "Cu Kalpha", "wavelength_angstrom": 1.5406,
            "license_status": "unknown",
            "redistribution_permission_status": "unknown",
            "review_status": "needs_review",
        })


def test_journey_2_measured_xrd_two_references_ambiguity_and_restart(tmp_path):
    store, project, material = _context(tmp_path, "Synthetic test XRD journey")
    raw_pattern = b"2theta,Counts\n20.0,100\n29.4,80\n55.0,-2\n"
    pattern = xrd_advisory.import_measured_pattern_csv(
        raw_pattern, source_filename="synthetic-measured.csv",
        metadata={
            "project_id": project.project_id, "material_id": material.material_id,
            "sample_id": "SYN-XRD-1", "radiation_source": "Cu Kalpha",
            "wavelength_angstrom": 1.5406, "instrument": "Synthetic test instrument",
            "method": "Synthetic test scan", "intensity_unit": "counts",
            "intensity_type": "background corrected",
        }, duplicate_policy=xrd_advisory.DUPLICATE_KEEP_ALL,
        user_peak_list=[20.0, 29.4, 55.0],
        peak_selection_provenance={"provided_by": ACTOR, "selected_at": WHEN})
    pattern_artifact = xrd_records.save_measured_pattern(
        store, project_id=project.project_id, material_id=material.material_id,
        pattern=pattern, creator=ACTOR, reason="Synthetic test measured import")
    ref_a = _xrd_reference(
        b"2theta,Intensity\n20.05,100\n29.35,60\n40,20\n",
        phase="Synthetic alpha", record_id="SYN-A")
    ref_b = _xrd_reference(
        b"2theta,Intensity\n20.02,100\n29.38,50\n45,20\n",
        phase="Synthetic beta", record_id="SYN-B")
    artifacts = [xrd_records.save_xrd_reference(
        store, project_id=project.project_id, material_id=material.material_id,
        reference=reference, creator=ACTOR, reason="Synthetic test user reference")
                 for reference in (ref_a, ref_b)]
    saved = xrd_records.save_tentative_match_run(
        store, project_id=project.project_id, material_id=material.material_id,
        pattern_artifact_id=pattern_artifact.artifact_id,
        reference_artifact_ids=[item.artifact_id for item in artifacts],
        tolerance=0.1, creator=ACTOR, reason="Synthetic test advisory comparison")
    reopened = xrd_records.load_tentative_match_run(
        workspace_store.WorkspaceStore(store.root), saved.run.run_id,
        project_id=project.project_id, material_id=material.material_id)
    assert reopened.pattern.pattern.source_sha256 == pattern.source_sha256
    assert len(reopened.references) == 2
    assert reopened.match_payload["overlap_ambiguity_count"] == 2
    assert reopened.match_payload["unmatched_measured"] == [55.0]
    text = json.dumps(reopened.match_payload).lower()
    assert all(term in text for term in (
        "tentative", "advisory", "possible match",
        "check against an appropriate reference source"))
    assert not re.search(r"\b(identified|confirmed phase|validated phase|quantified phase)\b", text)
    assert all(item.reference.license_status == "unknown" for item in reopened.references)


def test_journey_3_manual_evidence_review_revision_exports_and_run(tmp_path):
    store, project, material = _context(tmp_path, "Synthetic test evidence journey")
    draft = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id,
        _evidence_payload(), creator=ACTOR)
    evidence_review.link_material(store, draft.artifact_id)
    edited = evidence_review.edit_draft(
        store, draft.artifact_id, {"notes": "Edited structured synthetic note"},
        editor=ACTOR, reason="Synthetic test edit")
    submitted = evidence_review.submit_for_review(
        store, edited.artifact_id, submitter=ACTOR, reason="Synthetic test submission")
    reviewed = evidence_review.mark_reviewed(
        store, submitted.artifact_id, reviewer=ACTOR, reason="Synthetic source checked")
    run = phase3_artifacts.save_reviewed_evidence_run(
        store, reviewed.artifact_id, machine_id=machines.LITERATURE_ENGINE)
    revision = evidence_review.revise_evidence(
        store, reviewed.artifact_id, {"notes": "Revised synthetic interpretation"},
        creator=ACTOR, reason="Synthetic test revision")
    evidence_review.link_material(store, revision.artifact_id)
    assert revision.status == E.REVIEW_NEEDS_REVIEW
    assert evidence_review.get_evidence(store, reviewed.artifact_id).status == E.REVIEW_REVIEWED
    records = evidence_review.list_evidence(store, project_id=project.project_id)
    assert reviewed in records and revision in records
    assert "source_location" in evidence_review.export_json(records)
    assert "citation_title" in evidence_review.export_csv(records, E.SCHEMA_LEACHING)
    package = json.loads(evidence_review.export_package(records, E.SCHEMA_LEACHING))
    assert package["metadata"]["reviewed_count"] == 1
    reopened = workspace_store.WorkspaceStore(store.root)
    assert reopened.get_run(run.run_id).evidence_identity[0]["artifact_id"] == reviewed.artifact_id
    assert reopened.run_staleness(run)[0] is True
    assert evidence_review.list_evidence(
        reopened, project_id=project.project_id,
        review_status=E.REVIEW_REVIEWED) == [reviewed]


def test_journey_4_explicit_cfa_and_generic_plan_save_export_reopen(tmp_path):
    store, project, material = _context(tmp_path, "Synthetic test plan journey")
    cfa = plan_generator.build_cfa_preset_advisory(max_run_count=100)
    assert cfa["mode"] == "cfa_leaching_preset"
    assert cfa["duplicate_conditions_removed"] > 0
    generic_args = {
        "material_id": material.material_id,
        "goal": "Synthetic test non-CFA factor plan",
        "factors": {"temperature_C": [20, 20, 40], "binder_type": ["synthetic-B"]},
        "fixed_conditions": {"water_ratio": 0.4},
        "replicates": 2, "max_run_count": 8,
        "controls": [], "sample_prefix": "SYN", "experiment_date": "2026-08-29",
    }
    generic = plan_generator.build_generic_factor_plan(**generic_args)
    repeated = plan_generator.build_generic_factor_plan(**generic_args)
    assert generic["plan"].to_dict("records") == repeated["plan"].to_dict("records")
    assert generic["duplicate_conditions_removed"] == 1
    assert generic["run_count"] == 4
    rows = generic["plan"].to_dict("records")
    assert all(row[column] == "" for row in rows
               for column in plan_generator.GENERIC_MEASUREMENT_COLUMNS)
    assert all("NaOH_M" not in row and "fly_ash_type" not in row for row in rows)
    assert "sample_id" in plan_generator.plan_to_csv(generic["plan"])
    assert json.loads(plan_generator.plan_to_json(generic))["rows"] == rows
    result = runner.run_virtual_lab_machine(
        machines.EXPERIMENTAL_DESIGN,
        {"mode": "generic_user_defined", **generic_args})
    saved = phase3_artifacts.save_advisory_artifact_run(
        store, project_id=project.project_id, material_id=material.material_id,
        record_type=workspace_store.ARTIFACT_EXPERIMENT_PLAN,
        machine_id=machines.EXPERIMENTAL_DESIGN,
        input_snapshot={"mode": "generic_user_defined", **generic_args},
        payload=result.results,
        source_identity={"source_type": "synthetic_test_form",
                         "source_sha256": workspace_store.identity_hash(generic_args)},
        creator=ACTOR, reason="Synthetic test plan save", warnings=result.warnings,
        provenance=result.provenance)
    reopened = workspace_store.WorkspaceStore(store.root)
    assert reopened.get_run(saved.run.run_id).result_data["artifact_payload"]["rows"] == rows
    assert reopened.run_staleness(saved.run) == (False, [])


def test_journey_5_supplied_factor_sustainability_missing_zero_and_restart(tmp_path):
    store, project, material = _context(tmp_path, "Synthetic test sustainability journey")
    draft = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id,
        _evidence_payload("Synthetic test factor source"), creator=ACTOR)
    evidence_review.link_material(store, draft.artifact_id)
    submitted = evidence_review.submit_for_review(store, draft.artifact_id, submitter=ACTOR)
    reviewed = evidence_review.mark_reviewed(
        store, submitted.artifact_id, reviewer=ACTOR, reason="Synthetic factor checked")
    rows = [
        {"item": "Synthetic sourced input", "amount": 2, "amount_unit": "kg",
         "factor": 3, "factor_unit": "kg CO2e/kg",
         "factor_source": "Synthetic test evidence", "source_type": "literature_evidence",
         "evidence_id": reviewed.artifact_id, "boundary": "Synthetic gate"},
        {"item": "Synthetic explicit zero", "amount": 1, "amount_unit": "kg",
         "factor": 0, "factor_unit": "kg CO2e/kg",
         "factor_source": "Synthetic user assumption", "source_type": "user_assumption",
         "boundary": "Synthetic gate"},
        {"item": "Synthetic missing factor", "amount": 4, "amount_unit": "kg",
         "factor": None, "factor_unit": "kg CO2e/kg",
         "factor_source": "", "source_type": "user_assumption",
         "boundary": "Synthetic gate"},
    ]
    screen = sustainability_score.screen_inventory(rows)
    assert screen["totals"] == [{
        "boundary": "Synthetic gate", "result_unit": "kg CO2e", "total": 6.0,
        "sensitivity_min": None, "sensitivity_max": None}]
    by_item = {row["item"]: row for row in screen["contributions"]}
    assert by_item["Synthetic explicit zero"]["contribution"] == 0.0
    assert by_item["Synthetic missing factor"]["contribution"] is None
    assert "missing_factor" in sustainability_score.inventory_to_csv(screen)
    assert json.loads(sustainability_score.inventory_to_json(screen))["missing_factors"]
    evidence_dependency = phase3_artifacts.artifact_dependency(reviewed)
    saved = phase3_artifacts.save_advisory_artifact_run(
        store, project_id=project.project_id, material_id=material.material_id,
        record_type=workspace_store.ARTIFACT_SUSTAINABILITY_SCREEN,
        machine_id=machines.SUSTAINABILITY,
        input_snapshot={"mode": "user_inventory_screen", "inventory_rows": rows},
        payload=screen,
        source_identity={"source_type": "synthetic_test_inventory",
                         "source_sha256": workspace_store.identity_hash(rows)},
        creator=ACTOR, reason="Synthetic test screening save",
        warnings=["Screening only; Synthetic test data."],
        provenance={"fixture": "Synthetic test data"},
        evidence_identity=[evidence_dependency], dependency_artifacts=[reviewed])
    reopened = workspace_store.WorkspaceStore(store.root)
    run = reopened.get_run(saved.run.run_id)
    assert run.evidence_identity == [evidence_dependency]
    assert len(run.input_snapshot["artifact_identities"]) == 2
    assert reopened.run_staleness(run) == (False, [])
    language = json.dumps(screen).lower()
    assert "certified lca" not in language and "certified tea" not in language
    assert "economic feasibility result" not in language
