"""Phase 4 hosted authentication and durable tenant-isolation tests."""
from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from flyash_phreeqc_ml import config, run_manager, workspace_store as ws
from flyash_phreeqc_ml.security.identity import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_VIEWER,
    AuthenticationRequiredError,
    AuthorizationError,
    DeploymentMode,
    IdentityContext,
    bind_session_state,
    identity_context,
    local_single_user_identity,
    resolve_streamlit_identity,
    session_namespace,
)
from flyash_phreeqc_ml.simulation.run_registry import (
    SimulationRunRecord,
    SimulationRunRegistry,
)
from flyash_phreeqc_ml.storage_scope import StorageScope


def _hosted(subject: str, tenant: str, *roles: str) -> IdentityContext:
    return IdentityContext(subject, tenant, frozenset(roles or (ROLE_MEMBER,)),
                           DeploymentMode.HOSTED)


def _streamlit_user(**claims):
    return SimpleNamespace(user={"is_logged_in": True, "exp": time.time() + 3600, **claims})


def test_identity_context_is_deeply_immutable_and_key_free():
    identity = _hosted("subject-a", "tenant-a", ROLE_MEMBER)
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.tenant_id = "tenant-b"
    assert isinstance(identity.roles, frozenset)
    safe = identity.to_safe_dict()
    assert "subject-a" not in str(safe) and "tenant-a" not in str(safe)


def test_local_mode_is_explicit_and_preserves_historical_root(tmp_path):
    identity = local_single_user_identity()
    scope = StorageScope.for_identity(tmp_path / "workspace", identity)
    assert scope.is_local_compatibility_mode
    assert scope.tenant_root == (tmp_path / "workspace").absolute()
    assert scope.active_context_path.name == "active_context.json"
    assert ws.WorkspaceStore(tmp_path / "workspace").identity == identity


def test_hosted_authentication_and_authorization_fail_closed(monkeypatch):
    env = {"VLAB_DEPLOYMENT_MODE": "hosted"}
    with pytest.raises(AuthenticationRequiredError):
        resolve_streamlit_identity(SimpleNamespace(user={"is_logged_in": False}), environ=env)

    # OIDC authentication alone is not invite authorization.
    with pytest.raises(AuthorizationError):
        resolve_streamlit_identity(_streamlit_user(sub="a"), environ=env)

    expired = {**env, "VLAB_ALLOWED_SUBJECTS": "a"}
    with pytest.raises(AuthenticationRequiredError):
        resolve_streamlit_identity(
            SimpleNamespace(user={"is_logged_in": True, "sub": "a", "exp": time.time() - 1}),
            environ=expired,
        )


def test_hosted_anonymous_app_stops_before_store_and_upload_surfaces(monkeypatch):
    streamlit_test = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("VLAB_DEPLOYMENT_MODE", "hosted")
    for name in (
        "VLAB_ALLOWED_SUBJECTS", "VLAB_ALLOWED_TENANTS", "VLAB_VIEWER_SUBJECTS",
        "VLAB_VIEWER_TENANTS", "VLAB_MEMBER_SUBJECTS", "VLAB_MEMBER_TENANTS",
        "VLAB_ADMIN_SUBJECTS",
    ):
        monkeypatch.delenv(name, raising=False)
    app = Path(__file__).resolve().parents[1] / "app.py"
    rendered = streamlit_test.AppTest.from_file(app, default_timeout=90).run()
    assert rendered.exception is None or len(rendered.exception) == 0
    assert len(rendered.get("file_uploader")) == 0
    text = " ".join(str(item.value) for item in (*rendered.info, *rendered.error))
    assert "requires an invited account" in text

    source = app.read_text(encoding="utf-8")
    assert source.index("resolve_streamlit_identity(st)") < source.index(
        "product_shell.get_store(IDENTITY)")
    assert "st.login()" in source and "st.user" in (
        Path(__file__).resolve().parents[1] / "flyash_phreeqc_ml" / "security" /
        "identity.py").read_text(encoding="utf-8")


def test_hosted_legacy_surfaces_gate_before_any_global_reader(monkeypatch):
    from ui import compare_tab, export_tab, match_tab, validate_tab

    messages = []
    fake_streamlit = SimpleNamespace(warning=messages.append)
    monkeypatch.setattr(validate_tab, "st", fake_streamlit)
    monkeypatch.setattr(compare_tab, "st", fake_streamlit)
    monkeypatch.setattr(match_tab, "st", fake_streamlit)
    monkeypatch.setattr(export_tab, "st", SimpleNamespace(info=messages.append))

    def forbidden(*args, **kwargs):
        raise AssertionError("a legacy global reader/action was reached")

    monkeypatch.setattr(validate_tab, "_render_next_step", forbidden)
    monkeypatch.setattr(compare_tab, "_render_next_step", forbidden)
    monkeypatch.setattr(match_tab, "_render_next_step", forbidden)
    monkeypatch.setattr(validate_tab.run_manager, "load_run_config", forbidden)
    with identity_context(_hosted("member", "lab-a")):
        validate_tab.render("guessed-run", False)
        compare_tab.render("guessed-run")
        match_tab.render("guessed-run")
        export_tab._render_export_report("guessed-run")
    assert any("legacy Validate" in item for item in messages)
    assert any("legacy Compare" in item for item in messages)
    assert any("legacy Match" in item for item in messages)
    assert any("Legacy validation-report" in item for item in messages)


def test_hosted_store_never_falls_back_to_local_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("VLAB_DEPLOYMENT_MODE", "hosted")
    with pytest.raises(AuthenticationRequiredError):
        ws.WorkspaceStore(tmp_path / "workspace")
    with pytest.raises(AuthenticationRequiredError):
        StorageScope.for_identity(tmp_path / "workspace")


def test_hosted_invite_role_and_admin_are_separate_allowlists():
    st = _streamlit_user(sub="invited", roles=["admin", "member"])
    env = {"VLAB_DEPLOYMENT_MODE": "hosted", "VLAB_ALLOWED_SUBJECTS": "invited"}
    # An OIDC role claim and an invite do not confer application write rights.
    with pytest.raises(AuthorizationError):
        resolve_streamlit_identity(st, environ=env)

    viewer = resolve_streamlit_identity(
        st, environ={**env, "VLAB_VIEWER_SUBJECTS": "invited"})
    assert viewer.roles == frozenset({ROLE_VIEWER}) and not viewer.is_admin

    identity = resolve_streamlit_identity(
        st, environ={**env, "VLAB_MEMBER_SUBJECTS": "invited"})
    assert identity.roles == frozenset({ROLE_MEMBER}) and not identity.is_admin

    admin = resolve_streamlit_identity(
        st, environ={**env, "VLAB_ADMIN_SUBJECTS": "invited"})
    assert admin.is_admin and ROLE_ADMIN in admin.roles


def test_hosted_tenant_role_allowlist_still_requires_a_separate_invite():
    st = _streamlit_user(sub="user-a", organization="lab-a")
    role_only = {
        "VLAB_DEPLOYMENT_MODE": "hosted",
        "VLAB_OIDC_TENANT_CLAIM": "organization",
        "VLAB_MEMBER_TENANTS": "lab-a",
    }
    with pytest.raises(AuthorizationError, match="invite"):
        resolve_streamlit_identity(st, environ=role_only)
    member = resolve_streamlit_identity(
        st, environ={**role_only, "VLAB_ALLOWED_TENANTS": "lab-a"})
    assert member.roles == frozenset({ROLE_MEMBER})


def test_deterministic_tenant_roots_and_per_user_contexts(tmp_path):
    base = tmp_path / "workspace"
    a1 = StorageScope.for_identity(base, _hosted("alice", "lab-a"))
    a2 = StorageScope.for_identity(base, _hosted("anne", "lab-a"))
    b = StorageScope.for_identity(base, _hosted("bob", "lab-b"))
    assert a1.tenant_root == a2.tenant_root
    assert a1.active_context_path != a2.active_context_path
    assert a1.tenant_root != b.tenant_root
    assert "lab-a" not in str(a1.tenant_root) and "alice" not in str(a1.active_context_path)


def test_cross_tenant_listing_and_guessed_ids_fail(tmp_path):
    alice = ws.WorkspaceStore(tmp_path / "workspace", identity=_hosted("alice", "lab-a"))
    bob = ws.WorkspaceStore(tmp_path / "workspace", identity=_hosted("bob", "lab-b"))
    project = alice.create_project("Alice project")
    assert [item.project_id for item in alice.list_projects()] == [project.project_id]
    assert bob.list_projects() == []
    with pytest.raises(ws.RecordNotFoundError):
        bob.get_project(project.project_id)


def test_project_export_and_admin_deletion_are_tenant_scoped_and_hash_bound(tmp_path):
    root = tmp_path / "workspace"
    admin = ws.WorkspaceStore(root, identity=_hosted("admin-a", "lab-a", ROLE_ADMIN))
    project = admin.create_project("Deletion contract")
    material = admin.create_material(project.project_id, "Synthetic material")
    admin.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EVIDENCE,
        {"source": "synthetic"},
        payload={"title": "Synthetic evidence"},
    )
    admin.set_active_context(project.project_id, material.material_id, None)

    bundled_document, first = admin.export_project_bundle(project.project_id)
    assert first == admin.export_project_bytes(project.project_id)
    document = admin.export_project_document(project.project_id)
    assert bundled_document == json.loads(first.decode("utf-8"))
    assert document["project"]["project_id"] == project.project_id
    assert len(document["materials"]) == 1 and len(document["artifacts"]) == 1
    assert "admin-a" not in first.decode() and "lab-a" not in first.decode()

    other = ws.WorkspaceStore(root, identity=_hosted("admin-b", "lab-b", ROLE_ADMIN))
    with pytest.raises(ws.RecordNotFoundError):
        other.export_project_document(project.project_id)

    admin.archive_project(project.project_id, confirmation=project.project_id)
    current = admin.export_project_document(project.project_id)
    with pytest.raises(ws.ConfirmationRequiredError):
        admin.delete_project(
            project.project_id,
            export_sha256=document["export_sha256"],
            confirmation="wrong",
        )
    member = ws.WorkspaceStore(root, identity=_hosted("member-a", "lab-a", ROLE_MEMBER))
    with pytest.raises(AuthorizationError):
        member.delete_project(
            project.project_id,
            export_sha256=current["export_sha256"],
            confirmation=f"DELETE {project.project_id} {current['export_sha256']}",
        )
    deleted = admin.delete_project(
        project.project_id,
        export_sha256=current["export_sha256"],
        confirmation=f"DELETE {project.project_id} {current['export_sha256']}",
    )
    assert deleted["deleted_records"] == {
        "runs": 0, "artifacts": 1, "materials": 1, "projects": 1,
    }
    assert deleted["recoverable"] is False
    assert admin.list_projects(include_archived=True) == []
    assert admin.get_active_context()["active_project_id"] is None


def test_project_export_and_archive_preserve_validated_legacy_extension_fields(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Legacy export")
    material = store.create_material(project.project_id, "Legacy material")
    project_path = store._record_path("projects", project.project_id)
    material_path = store._record_path("materials", material.material_id)
    project_payload = json.loads(project_path.read_text(encoding="utf-8"))
    material_payload = json.loads(material_path.read_text(encoding="utf-8"))
    project_payload.update({
        "schema_version": 1,
        "legacy_extension": {"review_note": "preserve this accepted field"},
    })
    material_payload.update({
        "schema_version": 1,
        "legacy_extension": {"instrument_note": "preserve this too"},
    })
    project_path.write_text(json.dumps(project_payload), encoding="utf-8")
    material_path.write_text(json.dumps(material_payload), encoding="utf-8")

    store.archive_project(project.project_id, confirmation=project.project_id)
    exported = store.export_project_document(project.project_id)
    assert exported["project"]["legacy_extension"] == project_payload["legacy_extension"]
    assert exported["materials"][0]["legacy_extension"] == material_payload["legacy_extension"]


def test_archived_project_rejects_all_child_record_mutation(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Immutable archive")
    material = store.create_material(project.project_id, "Material")
    artifact = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EVIDENCE,
        {"source": "synthetic"},
        payload={"title": "Draft evidence"},
    )
    store.archive_project(project.project_id, confirmation=project.project_id)

    with pytest.raises(ws.WorkspaceStoreError, match="archived project"):
        store.update_project(project.project_id, name="Changed after archive")
    with pytest.raises(ws.WorkspaceStoreError, match="archived project"):
        store.update_material(material.material_id, description="Changed after archive")
    with pytest.raises(ws.WorkspaceStoreError, match="archived project"):
        store.create_artifact(
            project.project_id,
            material.material_id,
            ws.ARTIFACT_EVIDENCE,
            {"source": "synthetic"},
            payload={"title": "Late evidence"},
        )
    with pytest.raises(ws.WorkspaceStoreError, match="archived project"):
        store.update_artifact(artifact.artifact_id, payload={"title": "Changed"})


def test_local_project_deletion_clears_context_even_if_contexts_directory_exists(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Local deletion")
    store.archive_project(project.project_id, confirmation=project.project_id)
    store._path("contexts").mkdir(parents=True)
    store._active_context_path().write_text(json.dumps({
        "schema_version": ws.SCHEMA_VERSION,
        "active_project_id": project.project_id,
        "active_material_id": None,
        "active_run_id": None,
        "updated_at": "2026-08-29T00:00:00Z",
    }), encoding="utf-8")
    exported = store.export_project_document(project.project_id)
    store.delete_project(
        project.project_id,
        export_sha256=exported["export_sha256"],
        confirmation=f"DELETE {project.project_id} {exported['export_sha256']}",
    )
    assert store.get_active_context()["active_project_id"] is None


def test_account_state_deletion_removes_only_current_subject_context(tmp_path):
    root = tmp_path / "workspace"
    alice = ws.WorkspaceStore(root, identity=_hosted("alice", "lab-a", ROLE_MEMBER))
    project = alice.create_project("Shared tenant project")
    alice.set_active_context(project.project_id, None, None)
    account_id = alice.scope.session_namespace[:16]
    with pytest.raises(ws.ConfirmationRequiredError):
        alice.delete_current_account_state(confirmation="DELETE ACCOUNT wrong")
    result = alice.delete_current_account_state(
        confirmation=f"DELETE ACCOUNT {account_id}")
    assert result["active_context_deleted"] is True
    assert result["tenant_scientific_records_deleted"] is False
    assert alice.get_active_context()["active_project_id"] is None
    assert alice.get_project(project.project_id).name == "Shared tenant project"


def test_same_tenant_records_can_be_shared_but_active_context_is_per_user(tmp_path):
    alice = ws.WorkspaceStore(tmp_path / "workspace", identity=_hosted("alice", "lab-a"))
    anne = ws.WorkspaceStore(tmp_path / "workspace", identity=_hosted("anne", "lab-a"))
    project = alice.create_project("Shared lab project")
    alice.set_active_context(project.project_id, None, None)
    assert anne.get_project(project.project_id).name == "Shared lab project"
    assert anne.get_active_context()["active_project_id"] is None
    anne.set_active_context(project.project_id, None, None)
    assert anne.get_active_context()["active_project_id"] == project.project_id


def test_viewer_cannot_mutate_tenant_records_and_path_is_hidden(tmp_path):
    member = ws.WorkspaceStore(tmp_path / "workspace", identity=_hosted("member", "lab-a"))
    member.create_project("Readable")
    viewer = ws.WorkspaceStore(
        tmp_path / "workspace", identity=_hosted("viewer", "lab-a", ROLE_VIEWER))
    assert len(viewer.list_projects()) == 1
    with pytest.raises(AuthorizationError):
        viewer.create_project("Forbidden")
    diag = viewer.diagnostics()
    assert diag["storage_root"] == "tenant-scoped (path hidden)"
    assert str(tmp_path) not in str(diag)


def test_principal_change_clears_ai_history_uploads_and_scientific_caches():
    alice = _hosted("alice", "lab-a")
    bob = _hosted("bob", "lab-b")
    state = {"asst_state__run": "private transcript", "sim_previews": "private cache"}
    assert bind_session_state(state, alice) is True
    assert set(state) == {"_vl_identity_namespace"}
    state["compare_ai_msgs__run"] = ["private model response"]
    assert bind_session_state(state, alice) is False
    assert "compare_ai_msgs__run" in state
    assert bind_session_state(state, bob) is True
    assert set(state) == {"_vl_identity_namespace"}
    namespace = session_namespace(bob)
    assert state["_vl_identity_namespace"] == namespace
    assert "bob" not in namespace and "lab-b" not in namespace


def test_legacy_run_manager_is_tenant_scoped_and_same_name_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    alice = _hosted("alice", "lab-a")
    bob = _hosted("bob", "lab-b")
    with identity_context(alice):
        path_a = run_manager.create_run("shared-name", "lab_experiment")
        run_manager.append_lab_row("shared-name", {"sample_id": "A-private"})
    with identity_context(bob):
        assert run_manager.list_runs() == []
        with pytest.raises(run_manager.RunManagerError):
            run_manager.load_run_config("shared-name")
        path_b = run_manager.create_run("shared-name", "lab_experiment")
        run_manager.append_lab_row("shared-name", {"sample_id": "B-private"})
    assert path_a != path_b
    assert "lab-a" not in str(path_a) and "lab-b" not in str(path_b)
    with identity_context(alice):
        assert run_manager.read_data_file("shared-name")["sample_id"].tolist() == ["A-private"]
    with identity_context(bob):
        assert run_manager.read_data_file("shared-name")["sample_id"].tolist() == ["B-private"]


def test_legacy_run_manager_rejects_direct_path_and_oversized_name_probes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    with identity_context(_hosted("alice", "lab-a")):
        run_manager.create_run("known-run", "lab_experiment")
        for probe in ("../known-run", "/known-run", "..\\known-run", "known\x00run"):
            with pytest.raises(run_manager.RunManagerError):
                run_manager.load_run_config(probe)
        with pytest.raises(run_manager.RunManagerError, match="length"):
            run_manager.load_run_config("x" * 513)


def test_simulation_registry_blocks_guessed_ids_paths_and_cross_tenant_symlinks(tmp_path):
    alice = _hosted("alice", "lab-a")
    bob = _hosted("bob", "lab-b")
    base = tmp_path / "simulation-runs"
    reg_a = SimulationRunRegistry(base, identity=alice)
    reg_b = SimulationRunRegistry(base, identity=bob)
    record = SimulationRunRecord(run_id="same-id", created_at="2026-08-29T00:00:00Z")
    path_a = reg_a.save_run(record)
    assert reg_a.load_run("same-id")["run_id"] == "same-id"
    assert reg_b.load_run("same-id") is None and reg_b.list_runs() == []
    path_b = reg_b.save_run(record)
    assert path_a != path_b
    for probe in ("../same-id", "/tmp/same-id", "..\\same-id"):
        with pytest.raises(ValueError):
            reg_a.load_run(probe)
    link = reg_a.base_dir / "linked-run"
    try:
        link.symlink_to(path_b, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError):
        reg_a.load_run("linked-run")


def test_concurrent_tenant_simulation_runs_with_same_id_never_collide(tmp_path):
    tenants = [_hosted(f"user-{index}", f"tenant-{index}") for index in range(8)]
    base = tmp_path / "simulation-runs"

    def save(identity):
        registry = SimulationRunRegistry(base, identity=identity)
        path = registry.save_run(SimulationRunRecord(
            run_id="concurrent-run", created_at="2026-08-29T00:00:00Z"))
        return path, registry.load_run("concurrent-run")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, tenants))
    paths = [path for path, metadata in results if metadata["run_id"] == "concurrent-run"]
    assert len(paths) == len(tenants) and len(set(paths)) == len(tenants)
