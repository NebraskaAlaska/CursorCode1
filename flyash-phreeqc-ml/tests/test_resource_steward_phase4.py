from __future__ import annotations

import json
import inspect
from pathlib import Path

import pytest

from flyash_phreeqc_ml.resource_steward import main as steward_main
from flyash_phreeqc_ml.resources import (
    CatalogStore,
    ProposalStatus,
    RedistributionState,
    ResourceKind,
    ResourceManifest,
    ResourceSteward,
    RollbackState,
    StewardError,
)
from flyash_phreeqc_ml.resources.models import sha256_bytes


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
