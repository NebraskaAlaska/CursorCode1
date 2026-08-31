from __future__ import annotations

import json
import inspect
import os
from dataclasses import replace
from pathlib import Path

import pytest

from flyash_phreeqc_ml.resource_steward import main as steward_main
from flyash_phreeqc_ml.resources import (
    CatalogStore,
    CatalogError,
    CompatibilityStatus,
    DatabaseRegistryError,
    ExternalDatabaseImporter,
    ProposalStatus,
    RedistributionState,
    ResourceKind,
    ResourceManifest,
    ResourceSteward,
    RollbackState,
    StewardError,
    TestStatus as ResourceTestStatus,
    make_installation_id,
)
from flyash_phreeqc_ml.resources.models import sha256_bytes
from flyash_phreeqc_ml.simulation import phreeqc_executor


OLD_DATABASE = b"""SOLUTION_MASTER_SPECIES
Ca Ca+2 0 Ca 40
SOLUTION_SPECIES
Ca+2 = Ca+2
 log_k 0
PHASES
Calcite
CaCO3 = Ca+2 + CO3-2
log_k -8
END
"""
NEW_DATABASE = OLD_DATABASE.replace(b"END\n", b"Portlandite\nCa(OH)2 = Ca+2 + 2OH-\nlog_k 22\nEND\n")
SYNTHETIC_EXTENSION = b"""# Synthetic test fixture for use with phreeqc.dat
PHASES
WPI_Synthetic_Extension_Phase
H2O = H+ + OH-
log_k -14
"""
SYNTHETIC_EXTENSION_V2 = SYNTHETIC_EXTENSION.replace(b"-14", b"-13.5")


def _manifest(
    tmp_path: Path,
    *,
    version: str,
    source_hash: str,
    database_hash: str,
    filename: str,
) -> ResourceManifest:
    location = tmp_path / filename
    if not location.exists():
        location.write_bytes(OLD_DATABASE if version == "old" else NEW_DATABASE)
    return ResourceManifest(
        resource_id="wpi.phreeqc.database",
        installation_id=f"wpi.phreeqc.database-{version}-{database_hash[:12]}",
        resource_kind=ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        display_name="PHREEQC official database",
        provider="USGS",
        official_source_url="https://water.usgs.gov/water-resources/software/PHREEQC/",
        installed_version=version,
        archive_filename=filename,
        source_sha256=source_hash,
        database_sha256=database_hash,
        content_sha256=database_hash,
        install_path=str(location.resolve()),
        rights_notice="USGS rights notice reviewed",
        redistribution_state=RedistributionState.PERMITTED,
        rollback_state=RollbackState.CANDIDATE,
    )


def _install_fake_active_runtime(store: CatalogStore, tmp_path: Path) -> ResourceManifest:
    """Install a deterministic PHREEQC-shaped test double for Steward unit tests."""
    executable = tmp_path / "phreeqc-test-double"
    executable.write_text(
        "#!/bin/sh\n"
        "printf 'End of Run\\n' > \"$2\"\n"
        "printf 'pH\\n7\\n' > selected.out\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    digest = sha256_bytes(executable.read_bytes())
    manifest = ResourceManifest(
        resource_id="wpi.phreeqc.runtime",
        installation_id=f"wpi.phreeqc.runtime-test-{digest[:12]}",
        resource_kind=ResourceKind.PHREEQC_RUNTIME,
        display_name="PHREEQC test double",
        provider="test fixture",
        installed_version="test",
        source_sha256=digest,
        executable_sha256=digest,
        content_sha256=digest,
        install_path=str(executable.resolve()),
        rollback_state=RollbackState.CANDIDATE,
    )
    store.register(manifest)
    store.bootstrap_activate(manifest.resource_id, manifest.installation_id)
    return manifest


def _install_runtime_manifest(
    store: CatalogStore, executable: Path, *, version: str = "synthetic-test",
) -> ResourceManifest:
    digest = sha256_bytes(executable.read_bytes())
    manifest = ResourceManifest(
        resource_id="synthetic.phreeqc.runtime",
        installation_id=make_installation_id(
            "synthetic.phreeqc.runtime", version, digest),
        resource_kind=ResourceKind.PHREEQC_RUNTIME,
        display_name="Synthetic-test PHREEQC runtime",
        provider="Synthetic test fixture",
        installed_version=version,
        source_sha256=digest,
        executable_sha256=digest,
        content_sha256=digest,
        install_path=str(executable.resolve()),
        test_status=ResourceTestStatus.PASSED,
        standalone_test_status=ResourceTestStatus.PASSED,
        rollback_state=RollbackState.CANDIDATE,
    )
    store.register(manifest)
    store.bootstrap_activate(manifest.resource_id, manifest.installation_id)
    return manifest


def _install_base(
    store: CatalogStore,
    path: Path,
    *,
    resource_id: str = "synthetic.phreeqc.database.base",
    filename: str = "phreeqc.dat",
    family: str = "phreeqc",
    version: str = "synthetic-base-v1",
) -> ResourceManifest:
    digest = sha256_bytes(path.read_bytes())
    manifest = ResourceManifest(
        resource_id=resource_id,
        installation_id=make_installation_id(resource_id, version, digest),
        resource_kind=ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        display_name="Synthetic test base database",
        provider="Synthetic test fixture",
        installed_version=version,
        archive_filename=filename,
        source_sha256=digest,
        database_sha256=digest,
        content_sha256=digest,
        install_path=str(path.resolve()),
        database_filename=filename,
        database_family=family,
        compatibility_status=CompatibilityStatus.COMPATIBLE,
        test_status=ResourceTestStatus.PASSED,
        standalone_test_status=ResourceTestStatus.PASSED,
        rollback_state=RollbackState.CANDIDATE,
    )
    store.register(manifest)
    store.bootstrap_activate(manifest.resource_id, manifest.installation_id)
    return manifest


def _install_prior_extension(
    store: CatalogStore, tmp_path: Path, *, resource_id: str,
) -> ResourceManifest:
    path = tmp_path / "prior-Concrete_PHR.dat"
    path.write_bytes(SYNTHETIC_EXTENSION.replace(b"-14", b"-15"))
    digest = sha256_bytes(path.read_bytes())
    manifest = ResourceManifest(
        resource_id=resource_id,
        installation_id=make_installation_id(resource_id, "synthetic-prior", digest),
        resource_kind=ResourceKind.DATABASE_EXTENSION,
        display_name="Prior synthetic database extension",
        provider="Synthetic test fixture",
        installed_version="synthetic-prior",
        archive_filename="Concrete_PHR.dat",
        source_sha256=digest,
        database_sha256=digest,
        content_sha256=digest,
        install_path=str(path.resolve()),
        database_filename="Concrete_PHR.dat",
        database_family="phreeqc",
        requires_database_filename="phreeqc.dat",
        requires_database_family="phreeqc",
        standalone_test_status=ResourceTestStatus.NOT_APPLICABLE_REQUIRES_BASE,
        rollback_state=RollbackState.CANDIDATE,
    )
    store.register(manifest)
    store.bootstrap_activate(manifest.resource_id, manifest.installation_id)
    return manifest


def _prepare_extension_candidate(
    steward: ResourceSteward,
    *,
    raw: bytes = SYNTHETIC_EXTENSION,
    version: str = "synthetic-extension-v2",
):
    digest = sha256_bytes(raw)
    proposal = steward.check(
        resource_id="synthetic.phreeqc.database.extension",
        resource_kind=ResourceKind.DATABASE_EXTENSION,
        candidate_version=version,
        source_url="https://water.usgs.gov/phreeqc/Concrete_PHR.dat",
        archive_filename="Concrete_PHR.dat",
        expected_source_sha256=digest,
        rights_notice="Synthetic fixture rights; not a redistributed scientific database",
        redistribution_state=RedistributionState.PERMITTED,
    )
    proposal = steward.download_candidate(proposal.proposal_id, fetcher=lambda _: raw)
    proposal = steward.verify_candidate(proposal.proposal_id)
    return steward.build_candidate(proposal.proposal_id)


def test_steward_full_lifecycle_never_silently_promotes_and_can_rollback(
    tmp_path: Path, monkeypatch,
) -> None:
    # Model the canonical release image, whose active-runtime identity variables must not
    # contaminate the quarantined candidate integration subprocess.
    for name, value in {
        "VLAB_RESOURCE_CATALOG": "/release/resources/catalog.json",
        "VLAB_RESOURCE_BOOTSTRAP_RESULT": "/release/resources/bootstrap.json",
        "VLAB_ACTIVE_RUNTIME_INSTALLATION_ID": "release-runtime-installation",
        "VLAB_ACTIVE_DATABASE_INSTALLATION_ID": "release-database-installation",
        "VLAB_KNOWLEDGE_PACK_HASH": "1" * 64,
        "PHREEQC_RUNTIME_MANIFEST": "/release/runtime-manifest.json",
        "PHREEQC_RUNTIME_MANIFEST_ID": "release-runtime-manifest",
        "PHREEQC_RUNTIME_ID": "release-runtime",
        "PHREEQC_SOURCE_MANIFEST": "/release/source-manifest.json",
        "PHREEQC_SOURCE_MANIFEST_SHA256": "2" * 64,
        "PHREEQC_SOURCE_ID": "release-source",
        "PHREEQC_DATABASE_ID": "release-database",
        "PHREEQC_DATABASE_MANIFEST_ID": "release-database-manifest",
        "PHREEQC_DATABASE_SHA256": "3" * 64,
        "PHREEQC_DATABASE_VERSION": "release-version",
        "PHREEQC_CONTAINER_IMAGE_DIGEST": "sha256:" + "4" * 64,
        "PHREEQC_VERSION": "release-version",
    }.items():
        monkeypatch.setenv(name, value)
    store = CatalogStore(tmp_path / "store")
    old_hash = sha256_bytes(OLD_DATABASE)
    old = _manifest(
        tmp_path, version="old", source_hash=old_hash, database_hash=old_hash,
        filename="old.dat",
    )
    store.register(old)
    store.activate(old.resource_id, old.installation_id, proposal_id="initial")
    _install_fake_active_runtime(store, tmp_path)
    steward = ResourceSteward(store)
    source_hash = sha256_bytes(NEW_DATABASE)

    proposal = steward.check(
        resource_id=old.resource_id,
        resource_kind=ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        candidate_version="new",
        source_url="https://water.usgs.gov/phreeqc/new.dat",
        archive_filename="new.dat",
        expected_source_sha256=source_hash,
        rights_notice="USGS rights notice reviewed",
        redistribution_state=RedistributionState.PERMITTED,
    )
    assert proposal.status == ProposalStatus.PROPOSED
    assert store.get_active(old.resource_id).installation_id == old.installation_id

    proposal = steward.download_candidate(proposal.proposal_id, fetcher=lambda _: NEW_DATABASE)
    proposal = steward.verify_candidate(proposal.proposal_id)
    assert store.get_active(old.resource_id).installation_id == old.installation_id

    proposal = steward.build_candidate(proposal.proposal_id)
    candidate = store.get_installation(proposal.candidate_installation_id)
    assert [item.evidence_id for item in proposal.build_evidence] == [
        "builtin-database-parse-install"
    ]
    assert store.get_active(old.resource_id).installation_id == old.installation_id

    proposal = steward.test_candidate(proposal.proposal_id)
    assert [item.evidence_id for item in proposal.test_evidence] == [
        "builtin-database-load-selected-output",
        "builtin-project-executor-integration",
    ]
    comparison = steward.compare(proposal.proposal_id)
    assert comparison.candidate_sha256 == source_hash
    assert store.get_active(old.resource_id).installation_id == old.installation_id

    confirmation = f"PROMOTE {proposal.proposal_id} {proposal.candidate_sha256}"
    with pytest.raises(StewardError, match="human administrator"):
        steward.promote(
            proposal.proposal_id,
            candidate_sha256=proposal.candidate_sha256,
            admin_confirmation=confirmation,
            actor_role="admin",
            actor_type="llm",
        )
    with pytest.raises(StewardError, match="candidate hash"):
        steward.promote(
            proposal.proposal_id,
            candidate_sha256="0" * 64,
            admin_confirmation=confirmation,
            actor_role="admin",
        )

    proposal = steward.show(proposal.proposal_id)
    proposal = steward.promote(
        proposal.proposal_id,
        candidate_sha256=proposal.candidate_sha256,
        admin_confirmation=f"PROMOTE {proposal.proposal_id} {proposal.candidate_sha256}",
        actor_role="admin",
    )
    assert store.get_active(old.resource_id).installation_id == candidate.installation_id

    report_json, report_md = steward.export_report(proposal.proposal_id, tmp_path / "reports")
    assert json.loads(report_json.read_text(encoding="utf-8"))["status"] == "promoted"
    assert "Active environment changed automatically: `False`" in report_md.read_text(
        encoding="utf-8")

    with pytest.raises(StewardError, match="exact human administrator"):
        steward.rollback(
            proposal.proposal_id,
            admin_confirmation=f"ROLLBACK {proposal.proposal_id} {'0' * 64}",
            actor_role="admin",
        )
    assert store.get_active(old.resource_id).installation_id == candidate.installation_id

    rolled_back = steward.rollback(
        proposal.proposal_id,
        admin_confirmation=f"ROLLBACK {proposal.proposal_id} {proposal.candidate_sha256}",
        actor_role="admin",
    )
    assert rolled_back.status == ProposalStatus.ROLLED_BACK
    assert store.get_active(old.resource_id).installation_id == old.installation_id
    assert store.get_installation(candidate.installation_id).primary_sha256 == source_hash


def test_candidate_hash_mismatch_is_rejected_in_quarantine(tmp_path: Path) -> None:
    steward = ResourceSteward(CatalogStore(tmp_path / "store"))
    proposal = steward.check(
        resource_id="wpi.phreeqc.runtime",
        resource_kind=ResourceKind.PHREEQC_RUNTIME,
        candidate_version="9.9",
        source_url="https://water.usgs.gov/phreeqc/source.tar.gz",
        archive_filename="source.tar.gz",
        expected_source_sha256="0" * 64,
        rights_notice="USGS rights reviewed",
        redistribution_state=RedistributionState.PERMITTED,
    )
    downloaded = steward.download_candidate(proposal.proposal_id, fetcher=lambda _: b"not-zero")
    with pytest.raises(StewardError, match="does not match"):
        steward.verify_candidate(downloaded.proposal_id)
    assert steward.show(downloaded.proposal_id).status == ProposalStatus.REJECTED


def test_cli_exposes_closed_phase4_command_surface(tmp_path: Path, capsys) -> None:
    code = steward_main([
        "--store-root", str(tmp_path / "store"), "check",
        "--resource-id", "wpi.phreeqc.runtime",
        "--kind", "phreeqc_runtime",
        "--candidate-version", "3.8.6",
        "--source-url", "https://water.usgs.gov/phreeqc/source.tar.gz",
        "--archive-filename", "source.tar.gz",
    ])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "proposed"

    build_parameters = inspect.signature(ResourceSteward.build_candidate).parameters
    test_parameters = inspect.signature(ResourceSteward.test_candidate).parameters
    assert "manifest" not in build_parameters
    assert "evidence" not in build_parameters
    assert "builder" not in build_parameters
    assert "evidence" not in test_parameters
    assert "findings" not in test_parameters
    assert "tester" not in test_parameters

    with pytest.raises(SystemExit):
        steward_main([
            "--store-root", str(tmp_path / "store"), "build-candidate",
            "proposal-" + "0" * 24,
            "--manifest", "self-authored.json",
            "--evidence", "self-authored-evidence.json",
        ])


def test_candidate_content_secret_marker_is_rejected_without_disclosure(tmp_path: Path) -> None:
    steward = ResourceSteward(CatalogStore(tmp_path / "store"))
    raw = OLD_DATABASE + b"# api_key=ABCDEFGHIJKLMNOPQRSTUVWX\n"
    digest = sha256_bytes(raw)
    proposal = steward.check(
        resource_id="wpi.phreeqc.database",
        resource_kind=ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        candidate_version="secret-canary",
        source_url="https://water.usgs.gov/phreeqc/secret-canary.dat",
        archive_filename="secret-canary.dat",
        expected_source_sha256=digest,
        rights_notice="USGS rights reviewed",
        redistribution_state=RedistributionState.PERMITTED,
    )
    downloaded = steward.download_candidate(proposal.proposal_id, fetcher=lambda _: raw)
    with pytest.raises(StewardError, match="safety marker") as captured:
        steward.verify_candidate(downloaded.proposal_id)
    assert "ABCDEFGHIJKLMNOPQRSTUVWX" not in str(captured.value)
    assert steward.show(downloaded.proposal_id).status == ProposalStatus.REJECTED


def test_steward_ui_reports_candidate_read_only_without_changing_active(
    tmp_path: Path, monkeypatch,
) -> None:
    streamlit_test = pytest.importorskip("streamlit.testing.v1")
    store = CatalogStore(tmp_path / "store")
    old_hash = sha256_bytes(OLD_DATABASE)
    old = _manifest(
        tmp_path, version="3.8.5-legacy-review-fixture", source_hash=old_hash,
        database_hash=old_hash, filename="old-ui.dat",
    )
    store.register(old)
    store.activate(old.resource_id, old.installation_id, proposal_id="fixture-initial")
    candidate_hash = sha256_bytes(NEW_DATABASE)
    proposal = ResourceSteward(store).check(
        resource_id=old.resource_id,
        resource_kind=ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        candidate_version="3.8.6-17100",
        source_url="https://water.usgs.gov/water-resources/software/PHREEQC/",
        archive_filename="phreeqc-3.8.6-17100.tar.gz",
        expected_source_sha256=candidate_hash,
        rights_notice="USGS rights notice reviewed",
        redistribution_state=RedistributionState.PERMITTED,
    )
    monkeypatch.setenv("WPI_RESOURCE_ROOT", str(store.root))
    app = streamlit_test.AppTest.from_string(
        "from types import SimpleNamespace\n"
        "from ui.settings import _render_resource_steward\n"
        "_render_resource_steward(SimpleNamespace(is_hosted=False))\n",
        default_timeout=60,
    ).run()
    assert not app.exception
    rendered = " ".join(
        str(getattr(item, "value", ""))
        for group in (app.markdown, app.caption, app.info, app.warning, app.success)
        for item in group
    )
    assert "Database Steward" in rendered
    assert "Candidate update available" in rendered
    assert app.selectbox[0].value == proposal.proposal_id
    assert "Promotion remains blocked" in rendered
    assert not any(
        "promote" in button.label.lower() or "rollback" in button.label.lower()
        for button in app.button
    )
    assert store.get_active(old.resource_id).installation_id == old.installation_id


def test_database_extension_complete_synthetic_lifecycle_binds_both_identities(
    tmp_path: Path,
) -> None:
    store = CatalogStore(tmp_path / "store")
    _install_fake_active_runtime(store, tmp_path)
    base_path = tmp_path / "phreeqc.dat"
    base_path.write_bytes(OLD_DATABASE)
    base = _install_base(store, base_path)
    prior = _install_prior_extension(
        store, tmp_path, resource_id="synthetic.phreeqc.database.extension")
    steward = ResourceSteward(store)

    proposal = _prepare_extension_candidate(steward)
    assert proposal.status == ProposalStatus.BUILT
    assert store.get_active(proposal.resource_id).installation_id == prior.installation_id

    proposal = steward.test_candidate(proposal.proposal_id)
    assert proposal.status == ProposalStatus.TESTED
    assert [item.evidence_id for item in proposal.test_evidence] == [
        "builtin-extension-self-contained-compatibility",
        "builtin-project-executor-extension-integration",
        "builtin-extension-base-content-binding",
    ]
    provenance = json.loads(proposal.test_evidence[1].details)
    candidate = store.get_installation(proposal.candidate_installation_id)
    assert provenance["base"] == {
        "resource_id": base.resource_id,
        "installation_id": base.installation_id,
        "version": base.installed_version,
        "filename": "phreeqc.dat",
        "family": "phreeqc",
        "sha256": base.primary_sha256,
    }
    assert provenance["extension"]["resource_id"] == candidate.resource_id
    assert provenance["extension"]["installation_id"] == candidate.installation_id
    assert provenance["extension"]["sha256"] == candidate.primary_sha256
    assert provenance["combined_identity_sha256"] == candidate.combined_identity_sha256
    assert provenance["reviewed_input_sha256"]
    assert candidate.extension_for_resource_id == base.resource_id
    assert candidate.extension_for_installation_id == base.installation_id
    assert candidate.extension_base_version == base.installed_version
    assert candidate.extension_base_sha256 == base.primary_sha256
    assert candidate.requires_database_filename == "phreeqc.dat"
    assert candidate.requires_database_family == "phreeqc"

    comparison = steward.compare(proposal.proposal_id)
    assert comparison.candidate_installation_id == candidate.installation_id
    assert comparison.candidate_sha256 == candidate.primary_sha256
    proposal = steward.show(proposal.proposal_id)
    promoted = steward.promote(
        proposal.proposal_id,
        candidate_sha256=proposal.candidate_sha256,
        admin_confirmation=f"PROMOTE {proposal.proposal_id} {proposal.candidate_sha256}",
        actor_role="admin",
    )
    assert store.get_active(proposal.resource_id).installation_id == candidate.installation_id

    store.record_reference(candidate.installation_id, "synthetic-extension-run-001")
    rolled_back = steward.rollback(
        promoted.proposal_id,
        admin_confirmation=(
            f"ROLLBACK {promoted.proposal_id} {promoted.candidate_sha256}"),
        actor_role="admin",
    )
    assert rolled_back.status == ProposalStatus.ROLLED_BACK
    assert store.get_active(proposal.resource_id).installation_id == prior.installation_id
    final_state = store.load()
    assert final_state.references[candidate.installation_id] == (
        "synthetic-extension-run-001",)
    assert json.loads(steward.show(proposal.proposal_id).test_evidence[1].details) == provenance


def test_database_extension_without_declared_base_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-extension.dat"
    source.write_bytes(b"PHASES\nSynthetic_phase\nH2O = H+ + OH-\nlog_k -14\n")
    with pytest.raises(DatabaseRegistryError, match="no explicit base dependency"):
        ExternalDatabaseImporter(CatalogStore(tmp_path / "store")).import_database(
            source,
            resource_id="synthetic.phreeqc.database.extension",
            display_name="Synthetic extension without base",
            provider="Synthetic test fixture",
            installed_version="synthetic",
            rights_confirmed=True,
            rights_notice="Synthetic test fixture rights",
            redistribution_state=RedistributionState.PERMITTED,
            actor_role="admin",
            resource_kind=ResourceKind.DATABASE_EXTENSION,
        )


@pytest.mark.parametrize(
    ("filename", "family"),
    [("wrong.dat", "phreeqc"), ("phreeqc.dat", "wrong-family")],
)
def test_database_extension_incorrect_base_filename_or_family_fails(
    tmp_path: Path, filename: str, family: str,
) -> None:
    store = CatalogStore(tmp_path / "store")
    _install_fake_active_runtime(store, tmp_path)
    base_path = tmp_path / "base-bytes.dat"
    base_path.write_bytes(OLD_DATABASE)
    _install_base(store, base_path, filename=filename, family=family)
    steward = ResourceSteward(store)
    proposal = _prepare_extension_candidate(steward)
    with pytest.raises(StewardError, match="declared base is unavailable"):
        steward.test_candidate(proposal.proposal_id)


def test_database_extension_ambiguous_exact_bases_fail(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "store")
    _install_fake_active_runtime(store, tmp_path)
    for index in range(2):
        path = tmp_path / f"base-{index}.dat"
        path.write_bytes(OLD_DATABASE + f"# base {index}\n".encode())
        _install_base(
            store,
            path,
            resource_id=f"synthetic.phreeqc.database.base{index}",
            filename="phreeqc.dat",
            family="phreeqc",
            version=f"synthetic-base-{index}",
        )
    steward = ResourceSteward(store)
    proposal = _prepare_extension_candidate(steward)
    with pytest.raises(StewardError, match="declared base is ambiguous"):
        steward.test_candidate(proposal.proposal_id)


@pytest.mark.parametrize("changed_target", ["extension", "base"])
def test_database_extension_changed_candidate_or_base_bytes_fail(
    tmp_path: Path, changed_target: str,
) -> None:
    store = CatalogStore(tmp_path / "store")
    _install_fake_active_runtime(store, tmp_path)
    base_path = tmp_path / "phreeqc.dat"
    base_path.write_bytes(OLD_DATABASE)
    _install_base(store, base_path)
    steward = ResourceSteward(store)
    proposal = _prepare_extension_candidate(steward)
    candidate = store.get_installation(proposal.candidate_installation_id)
    target = Path(candidate.install_path) if changed_target == "extension" else base_path
    target.write_bytes(target.read_bytes() + b"# changed after identity verification\n")
    with pytest.raises(StewardError, match="bytes changed after identity verification"):
        steward.test_candidate(proposal.proposal_id)


def test_tested_extension_manifest_cannot_rebind_to_replacement_base(
    tmp_path: Path,
) -> None:
    store = CatalogStore(tmp_path / "store")
    _install_fake_active_runtime(store, tmp_path)
    base_path = tmp_path / "phreeqc.dat"
    base_path.write_bytes(OLD_DATABASE)
    _install_base(store, base_path)
    steward = ResourceSteward(store)
    proposal = steward.test_candidate(_prepare_extension_candidate(steward).proposal_id)
    candidate = store.get_installation(proposal.candidate_installation_id)
    with pytest.raises(CatalogError, match="rebind an already tested database extension"):
        store.replace_manifest(replace(
            candidate,
            extension_for_installation_id=(
                candidate.extension_for_installation_id + ".other"),
        ))


def test_real_phreeqc_extension_lifecycle_is_synthetic_and_isolated(
    tmp_path: Path,
) -> None:
    required = os.environ.get("PHREEQC_INTEGRATION_REQUIRED") == "1"
    available = phreeqc_executor.check_availability()
    if not available.can_run or available.environment_identity is None:
        if required:
            pytest.fail(available.message)
        pytest.skip("real PHREEQC runtime is not configured")
    executable = Path(available.environment_identity.executable.resolved_path)
    database = Path(available.environment_identity.database.resolved_path)
    if database.name != "phreeqc.dat":
        if required:
            pytest.fail("release integration requires phreeqc.dat as the exact base")
        pytest.skip("configured real database is not phreeqc.dat")

    store = CatalogStore(tmp_path / "isolated-real-extension-store")
    _install_runtime_manifest(store, executable, version="real-runtime-test-fixture")
    base = _install_base(
        store,
        database,
        resource_id="synthetic.real.phreeqc.base",
        filename="phreeqc.dat",
        family="phreeqc",
        version="exact-configured-base",
    )
    prior = _install_prior_extension(
        store, tmp_path, resource_id="synthetic.phreeqc.database.extension")
    steward = ResourceSteward(store)
    proposal = _prepare_extension_candidate(
        steward, raw=SYNTHETIC_EXTENSION_V2, version="synthetic-real-smoke-v2")
    tested = steward.test_candidate(proposal.proposal_id)
    candidate = store.get_installation(tested.candidate_installation_id)
    assert candidate.extension_for_installation_id == base.installation_id
    assert candidate.base_include_test_status == ResourceTestStatus.PASSED
    tested = steward.show(tested.proposal_id)
    promoted = steward.promote(
        tested.proposal_id,
        candidate_sha256=tested.candidate_sha256,
        admin_confirmation=(
            f"PROMOTE {tested.proposal_id} {tested.candidate_sha256}"),
        actor_role="admin",
    )
    rolled_back = steward.rollback(
        promoted.proposal_id,
        admin_confirmation=(
            f"ROLLBACK {promoted.proposal_id} {promoted.candidate_sha256}"),
        actor_role="admin",
    )
    assert rolled_back.status == ProposalStatus.ROLLED_BACK
    assert store.get_active(candidate.resource_id).installation_id == prior.installation_id
    assert Path(base.install_path).read_bytes() == database.read_bytes()
