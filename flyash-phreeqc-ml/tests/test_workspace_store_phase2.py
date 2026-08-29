"""Phase 2 durable workspace, identity, isolation, and storage-safety coverage."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from flyash_phreeqc_ml import workspace_store as ws


@pytest.fixture()
def store(tmp_path):
    return ws.WorkspaceStore(tmp_path / "workspace")


def _project_material(store):
    project = store.create_project("Synthetic test project", description="test-only")
    material = store.create_material(
        project.project_id, "Synthetic test material", material_type="test fixture",
        composition=[{"component": "CaO", "value": 1.0, "unit": "wt%"}],
        composition_provenance={"source_type": "synthetic_demo"},
        assumptions=["synthetic test-only assumption"],
    )
    return project, material


def test_project_create_read_update_list_and_archive_confirmation(store):
    project = store.create_project("Project / unsafe display name", description="before")
    assert project.project_id.startswith(ws.PROJECT_PREFIX)
    assert "/" not in project.project_id
    assert store.get_project(project.project_id).name == "Project / unsafe display name"
    assert [item.project_id for item in store.list_projects()] == [project.project_id]
    updated = store.update_project(project.project_id, name="Renamed", description="after",
                                   status="planning")
    assert updated.name == "Renamed"
    with pytest.raises(ws.ConfirmationRequiredError):
        store.archive_project(project.project_id, confirmation="Renamed")
    archived = store.archive_project(project.project_id, confirmation=project.project_id)
    assert archived.archived and archived.status == "archived"
    assert store.list_projects() == []
    assert store.list_projects(include_archived=True)[0].project_id == project.project_id


def test_material_round_trip_revision_and_restart_persistence(store):
    project, material = _project_material(store)
    reopened = ws.WorkspaceStore(store.root)
    loaded = reopened.get_material(material.material_id)
    assert loaded.to_dict() == material.to_dict()
    admin = reopened.update_material(material.material_id, description="display-only change")
    assert admin.revision == material.revision
    changed = reopened.update_material(
        material.material_id,
        composition=[{"component": "CaO", "value": 2.0, "unit": "wt%"}],
        composition_provenance={"source_type": "synthetic_demo", "source_reference": "fixture"},
    )
    assert changed.revision == material.revision + 1
    assert changed.composition_revision == material.composition_revision + 1
    assert reopened.list_materials(project.project_id)[0].material_id == material.material_id
    on_disk = json.loads((store.root / "materials" / f"{material.material_id}.json").read_text())
    assert on_disk == changed.to_dict()


def test_run_identity_listing_and_no_immediate_staleness(store):
    project, material = _project_material(store)
    snapshot = {"measured_peaks": [20.0, 30.0], "fixture": "synthetic_test_only"}
    run = store.create_run(
        project.project_id, material.material_id, "xrd_advisory", snapshot,
        status="advisory", output_type="advisory_interpretation",
        epistemic_type="advisory_interpretation",
        result_data={"candidate_checklist": []}, warnings=["tentative only"],
        validation_state="not_applicable",
    )
    assert run.input_hash == ws.identity_hash(snapshot)
    assert store.get_run(run.run_id).to_dict() == run.to_dict()
    assert store.run_staleness(run) == (False, [])
    assert store.current_runs(project_id=project.project_id,
                              material_id=material.material_id)[0].run_id == run.run_id
    assert store.get_material(material.material_id).associated_run_ids == [run.run_id]


def test_scientific_material_change_marks_old_run_historical(store):
    project, material = _project_material(store)
    run = store.create_run(
        project.project_id, material.material_id, "experimental_design_assistant",
        {"goal": "synthetic test plan"}, status="advisory",
        output_type="advisory_interpretation", epistemic_type="advisory_interpretation")
    store.update_material(material.material_id,
                          assumptions=["changed synthetic test-only assumption"])
    stale, reasons = store.run_staleness(run)
    assert stale
    assert "material revision changed" in reasons
    assert "assumption/process revision changed" in reasons
    assert store.current_runs(project_id=project.project_id, material_id=material.material_id) == []
    assert store.get_run(run.run_id).input_snapshot == {"goal": "synthetic test plan"}


def test_project_isolation_and_cross_binding_fail_closed(store):
    p1, m1 = _project_material(store)
    p2 = store.create_project("Second synthetic project")
    m2 = store.create_material(p2.project_id, "Second synthetic material")
    run = store.create_run(
        p1.project_id, m1.material_id, "xrd_advisory", {"phases": ["Calcite"]},
        status="advisory", output_type="advisory_interpretation",
        epistemic_type="advisory_interpretation")
    assert [item.run_id for item in store.list_runs(project_id=p1.project_id)] == [run.run_id]
    assert store.list_runs(project_id=p2.project_id) == []
    with pytest.raises(ws.WorkspaceStoreError):
        store.create_run(p1.project_id, m2.material_id, "xrd_advisory", {}, status="blocked",
                         output_type="advisory_interpretation",
                         epistemic_type="advisory_interpretation")
    with pytest.raises(ws.WorkspaceStoreError):
        store.set_active_context(p2.project_id, m1.material_id, None)


def test_active_context_survives_store_restart(store):
    project, material = _project_material(store)
    run = store.create_run(
        project.project_id, material.material_id, "xrd_advisory", {}, status="advisory",
        output_type="advisory_interpretation", epistemic_type="advisory_interpretation")
    expected = store.set_active_context(project.project_id, material.material_id, run.run_id)
    reloaded = ws.WorkspaceStore(store.root).get_active_context()
    assert reloaded == expected


@pytest.mark.parametrize("bad_id", ["../project", "/tmp/project", "prj_bad", "", "prj_" + "g" * 32])
def test_path_traversal_and_malformed_ids_rejected(store, bad_id):
    with pytest.raises(ws.UnsafePathError):
        store.get_project(bad_id)


def test_duplicate_unknown_and_future_schema_behavior(store):
    fixed = "prj_" + "a" * 32
    store.create_project("First", project_id=fixed)
    with pytest.raises(ws.DuplicateRecordError):
        store.create_project("Second", project_id=fixed)
    with pytest.raises(ws.RecordNotFoundError):
        store.get_material("mat_" + "b" * 32)
    future = store.root / "projects" / ("prj_" + "c" * 32 + ".json")
    future.write_text(json.dumps({"schema_version": ws.SCHEMA_VERSION + 1,
                                  "project_id": future.stem}), encoding="utf-8")
    with pytest.raises(ws.UnsupportedSchemaError):
        store.get_project(future.stem)


def test_malformed_json_and_filename_identity_rejected(store):
    project_dir = store.root / "projects"
    project_dir.mkdir(parents=True)
    bad_id = "prj_" + "d" * 32
    (project_dir / f"{bad_id}.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError):
        store.get_project(bad_id)
    mismatch_id = "prj_" + "e" * 32
    (project_dir / f"{mismatch_id}.json").write_text(json.dumps({
        "schema_version": 1, "project_id": "prj_" + "f" * 32, "name": "mismatch"}),
        encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError):
        store.get_project(mismatch_id)


def test_atomic_replace_failure_preserves_existing_record(store, monkeypatch):
    project = store.create_project("Original")
    original = (store.root / "projects" / f"{project.project_id}.json").read_bytes()
    real_replace = os.replace

    def fail_replace(source, destination):
        if Path(destination).name == f"{project.project_id}.json":
            raise OSError("synthetic interrupted write")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic interrupted write"):
        store.update_project(project.project_id, name="Would overwrite")
    assert (store.root / "projects" / f"{project.project_id}.json").read_bytes() == original
    assert not list((store.root / "projects").glob(".tmp-*.json"))


def test_stale_temp_cleanup_and_symlink_escape_rejected(store, tmp_path):
    project_dir = store.root / "projects"
    project_dir.mkdir(parents=True)
    stale = project_dir / ".tmp-abandoned.json"
    stale.write_text("partial", encoding="utf-8")
    assert store.cleanup_stale_temps() == 1
    assert not stale.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    material_dir = store.root / "materials"
    try:
        material_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    project = store.create_project("Safe")
    with pytest.raises(ws.UnsafePathError):
        store.create_material(project.project_id, "Cannot escape")


def test_nonfinite_secret_like_and_unknown_machine_inputs_rejected(store):
    project, material = _project_material(store)
    with pytest.raises(ws.MalformedRecordError):
        store.create_run(project.project_id, material.material_id, "xrd_advisory",
                         {"value": float("nan")}, status="advisory",
                         output_type="advisory_interpretation",
                         epistemic_type="advisory_interpretation")
    with pytest.raises(ws.MalformedRecordError, match="secret-like"):
        store.update_project(project.project_id, metadata={"api_key": "do-not-store"})
    with pytest.raises(ws.MalformedRecordError, match="unknown machine"):
        store.create_run(project.project_id, material.material_id, "invented_machine", {},
                         status="unknown", output_type="advisory_interpretation",
                         epistemic_type="advisory_interpretation")


def test_serialization_is_deterministic(store):
    project = store.create_project("Deterministic", metadata={"z": 1, "a": 2})
    path = store.root / "projects" / f"{project.project_id}.json"
    first = path.read_text(encoding="utf-8")
    store.update_project(project.project_id, metadata={"a": 2, "z": 1})
    second = path.read_text(encoding="utf-8")
    assert first == second
