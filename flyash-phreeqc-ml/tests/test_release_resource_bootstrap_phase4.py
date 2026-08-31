from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from flyash_phreeqc_ml.resource_steward import main as steward_main
from flyash_phreeqc_ml.resources import (
    CatalogStore,
    CompatibilityStatus,
    KnowledgeFactType,
    KnowledgePack,
    RedistributionState,
    ReleaseBootstrapError,
    ResourceKind,
    ResourceManifest,
    RollbackState,
    TestStatus as ResourceTestStatus,
    bootstrap_release_resources,
    default_phreeqc_release_notes_path,
    load_official_database_registry,
    load_phreeqc_release_notes,
    make_installation_id,
    parse_database_file,
)
from flyash_phreeqc_ml.resources.models import canonical_hash, sha256_bytes


PHREEQC_DAT = b"""SOLUTION_MASTER_SPECIES
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
CONCRETE_EXTENSION = b"""# INCLUDE$ add-on for use with phreeqc.dat
PHASES
Portlandite
Ca(OH)2 = Ca+2 + 2OH-
log_k 22
"""

EXPECTED_OFFICIAL_HASHES = {
    "Amm.dat": "eaa4cede7a907447156f4b5ff53880ac693b2c7de6765198eb048addb2dfcf83",
    "ColdChem.dat": "e03e02c35ed15796d7c85213ad6280e7585ee8c74909f739950f4d07e5337f7d",
    "Concrete_PHR.dat": "ffe41089321b5be71eb76be57c0838f122e858e62dbf5ec5725e01adedcc2c02",
    "Concrete_PZ.dat": "ef0f8de173b30609c50bb405c04f6447a6d0382f667536571ea62dda953aebaf",
    "Kinec.v2.dat": "7ee972347f8791c900adbf86cffbb350e86ccfec3a7aa7160683ee9864f516a8",
    "Kinec_v3.dat": "5aa68b4b86503d7ef592c36ef0b3d8534ddec1dbecf0a9087e51164f0adf2c77",
    "PHREEQC_ThermoddemV1.10_15Dec2020.dat": "cfb1dbe03bc2744e622ee9544be628d051beda4f0de8a218afeddd62ad781aee",
    "Tipping_Hurley.dat": "0725d7a31346e398d3b3361eb2ea19dfd3b6f0aaf8bab2670c3af7f42ea77006",
    "core10.dat": "9492c1bd696141c5e2e6b17eee66b9dcf8fdbb24e16f2a365de19424248fcca1",
    "frezchem.dat": "a22b63e73080d9a6666469c6a9ea0caaadf4f2aee5396c99ffbfa477c1099d7a",
    "iso.dat": "ee76b7cc9f12adbf61226b0519db3279ea875df597a3baa339985352d98b72c0",
    "llnl.dat": "dbc108236b91b7618b5362e64c3968123bb7d969270cbc7129673b887397e78a",
    "minteq.dat": "b098ba653279256c05e7ac2ffa698529442f06d0bbb82a6f3ef2b8c309101f7a",
    "minteq.v4.dat": "a93914e63b0c616f8506cc92739188e50c5b9a15c33b111979652f308457f4ac",
    "phreeqc.dat": "59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10",
    "phreeqc_rates.dat": "8e348a8562bb5c43525722ee082cfb2cea01909ebc6c39f1b82870b31353b7a0",
    "pitzer.dat": "06ab2debc0cdb333598118df953165499c2f762a79de5f2df55dec6b78b02589",
    "sit.dat": "d7cbc6d459fd144b617efcf4d4f18ae68c53acd4ae873df16a305b74efac4155",
    "wateq4f.dat": "7816c92eadd28f7e4703efa467a038a197748cd38f410d81aa7909fde1c8912f",
}


def _record(path: Path, *, resource_id: str) -> dict:
    summary = parse_database_file(path)
    extension = summary.is_database_extension
    return {
        "filename": path.name,
        "resource_id": resource_id,
        "resource_kind": ("database_extension" if extension
                          else "phreeqc_official_database"),
        "database_sha256": summary.source_sha256,
        "byte_size": summary.byte_size,
        "database_family": summary.detected_family,
        "sections": list(summary.sections),
        "master_species_count": len(summary.master_species),
        "solution_species_count": len(summary.solution_species),
        "phase_count": len(summary.phases),
        "master_species_names_sha256": canonical_hash(list(summary.master_species)),
        "solution_species_names_sha256": canonical_hash(list(summary.solution_species)),
        "phase_names_sha256": canonical_hash(list(summary.phases)),
        "parsed_summary_sha256": canonical_hash(summary.to_dict()),
        "compatibility_status": "compatible",
        "test_status": ("not_applicable_requires_base" if extension else "passed"),
        "standalone_test_status": (
            "not_applicable_requires_base" if extension else "passed"),
        "base_include_test_status": "passed" if extension else "not_tested",
        "requires_database_filename": summary.requires_database_filename,
        "requires_database_family": summary.requires_database_family,
        "limitations": (["Reviewed include-only add-on."] if extension
                        else ["Software syntax/load smoke only."]),
    }


def _release_fixture(tmp_path: Path):
    executable = tmp_path / "phreeqc"
    executable.write_bytes(b"reviewed executable")
    executable.chmod(0o755)
    executable_hash = sha256_bytes(executable.read_bytes())
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    (database_dir / "phreeqc.dat").write_bytes(PHREEQC_DAT)
    (database_dir / "Concrete_PHR.dat").write_bytes(CONCRETE_EXTENSION)
    records = sorted([
        _record(
            database_dir / "Concrete_PHR.dat",
            resource_id="test.usgs.database.concrete-phr",
        ),
        _record(database_dir / "phreeqc.dat", resource_id="test.usgs.database.phreeqc"),
    ], key=lambda item: item["filename"])
    source_hash = "a" * 64
    registry = {
        "schema": "wpi.virtual-lab.usgs-phreeqc-database-registry",
        "version": 1,
        "phreeqc_version": "3.8.6-17100",
        "source_archive_url": "https://water.usgs.gov/phreeqc/source.tar.gz",
        "source_archive_sha256": source_hash,
        "official_release_page": "https://water.usgs.gov/phreeqc/",
        "rights_notice": "Reviewed USGS notice",
        "rights_notice_sha256": "b" * 64,
        "database_count": len(records),
        "databases": records,
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    release_notes = {
        "schema": "wpi.virtual-lab.phreeqc-release-notes",
        "version": 1,
        "artifact_id": "usgs.phreeqc.release-notes",
        "artifact_version": registry["phreeqc_version"],
        "repository_path": "release/PHREEQC_RELEASE_NOTES_3.8.6-17100.json",
        "provider": "U.S. Geological Survey",
        "official_release_page": registry["official_release_page"],
        "source_archive_url": registry["source_archive_url"],
        "archive_filename": "source.tar.gz",
        "reviewed_on": "2026-08-29",
        "release_metadata": [
            "Official fixture metadata identifies PHREEQC 3.8.6-17100.",
            "The fixture records a source archive, databases, examples, and documentation.",
        ],
        "limitations": [
            "Release metadata is not experimental validation.",
            "The fixture does not invent an upstream change list.",
        ],
        "citation": {
            "title": "Synthetic official-release fixture",
            "authors": ["U.S. Geological Survey"],
            "year": None,
            "doi": "",
            "url": registry["official_release_page"],
            "source_location": "test fixture release listing",
            "extraction_confidence": 1.0,
        },
    }
    notes_path = tmp_path / "PHREEQC_RELEASE_NOTES_3.8.6-17100.json"
    notes_path.write_text(json.dumps(release_notes, sort_keys=True), encoding="utf-8")
    runtime = ResourceManifest(
        resource_id="test.usgs.runtime",
        installation_id=make_installation_id(
            "test.usgs.runtime", "3.8.6-17100", executable_hash),
        resource_kind=ResourceKind.PHREEQC_RUNTIME,
        display_name="Test PHREEQC runtime",
        provider="USGS",
        official_source_url="https://water.usgs.gov/phreeqc/source.tar.gz",
        discovered_version="3.8.6-17100",
        installed_version="3.8.6-17100",
        source_sha256=source_hash,
        executable_sha256=executable_hash,
        content_sha256=executable_hash,
        install_path=str(executable.resolve()),
        rights_notice="Reviewed USGS notice",
        redistribution_state=RedistributionState.PERMITTED,
        compatibility_status=CompatibilityStatus.COMPATIBLE,
        test_status=ResourceTestStatus.PASSED,
        standalone_test_status=ResourceTestStatus.PASSED,
        rollback_state=RollbackState.CANDIDATE,
    )
    return executable, database_dir, registry_path, runtime


def test_tracked_official_registry_pins_all_19_reviewed_databases() -> None:
    registry, registry_file_hash = load_official_database_registry()
    records = {item.filename: item for item in registry.databases}
    assert registry.phreeqc_version == "3.8.6-17100"
    assert len(records) == 19
    assert {name: item.database_sha256 for name, item in records.items()} \
        == EXPECTED_OFFICIAL_HASHES
    assert sum(item.standalone_test_status == ResourceTestStatus.PASSED
               for item in records.values()) == 17
    assert records["Concrete_PHR.dat"].requires_database_filename == "phreeqc.dat"
    assert records["Concrete_PZ.dat"].requires_database_filename == "pitzer.dat"
    assert all(records[name].base_include_test_status == ResourceTestStatus.PASSED
               for name in ("Concrete_PHR.dat", "Concrete_PZ.dat"))
    assert len(registry_file_hash) == 64


def test_tracked_release_notes_artifact_is_versioned_and_bound_to_official_registry() -> None:
    registry, _registry_hash = load_official_database_registry()
    path = default_phreeqc_release_notes_path()
    artifact, artifact_hash = load_phreeqc_release_notes(path)
    assert artifact.artifact_id == "usgs.phreeqc.release-notes"
    assert artifact.artifact_version == registry.phreeqc_version
    assert artifact.official_release_page == registry.official_release_page
    assert artifact.source_archive_url == registry.source_archive_url
    assert artifact.archive_filename == "phreeqc-3.8.6-17100.tar.gz"
    assert artifact.repository_path == "release/PHREEQC_RELEASE_NOTES_3.8.6-17100.json"
    assert artifact_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.limitations


def test_bootstrap_registers_side_by_side_activates_only_defaults_and_is_idempotent(
    tmp_path: Path,
) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    notes_path = tmp_path / "PHREEQC_RELEASE_NOTES_3.8.6-17100.json"
    store = CatalogStore(tmp_path / "store")
    first = bootstrap_release_resources(
        store,
        runtime_manifest=runtime,
        executable_path=executable,
        database_directory=database_dir,
        registry_path=registry_path,
    )
    first_state = store.load()
    assert len(first.registered_database_installation_ids) == 2
    assert len(first_state.resources) == 4
    assert set(first_state.active) == {
        runtime.resource_id,
        "test.usgs.database.phreeqc",
        "wpi.phreeqc.knowledge-pack",
    }
    assert all(item.action == "bootstrap" for item in first_state.activation_history)
    assert all(not item.proposal_id for item in first_state.activation_history)
    assert Path(first.knowledge_pack_path).is_file()
    pack = KnowledgePack.from_dict(json.loads(
        Path(first.knowledge_pack_path).read_text(encoding="utf-8")))
    knowledge_manifest = store.get_active("wpi.phreeqc.knowledge-pack")
    assert knowledge_manifest is not None
    assert knowledge_manifest.source_sha256 == pack.pack_hash
    assert knowledge_manifest.content_sha256 == hashlib.sha256(
        Path(first.knowledge_pack_path).read_bytes()).hexdigest()
    assert pack.catalog_hash == first_state.knowledge_projection_hash
    release_facts = [
        fact for fact in pack.query(KnowledgeFactType.DOCUMENTATION_STATEMENT)
        if fact.source_artifacts
    ]
    assert len(release_facts) == 1
    artifact = release_facts[0].source_artifacts[0]
    assert artifact.artifact_version == "3.8.6-17100"
    assert artifact.sha256 == hashlib.sha256(notes_path.read_bytes()).hexdigest()
    assert release_facts[0].citation.url == "https://water.usgs.gov/phreeqc/"

    second = bootstrap_release_resources(
        store,
        runtime_manifest=runtime,
        executable_path=executable,
        database_directory=database_dir,
        registry_path=registry_path,
    )
    assert second.to_dict() == first.to_dict()
    assert store.load().generation == first_state.generation


def test_bootstrap_refuses_to_replace_an_active_runtime_before_registration(
    tmp_path: Path,
) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    store = CatalogStore(tmp_path / "store")
    prior_hash = "c" * 64
    prior_runtime = replace(
        runtime,
        installation_id=make_installation_id(runtime.resource_id, "3.8.5", prior_hash),
        discovered_version="3.8.5",
        installed_version="3.8.5",
        source_sha256="d" * 64,
        executable_sha256=prior_hash,
        content_sha256=prior_hash,
    )
    store.register(prior_runtime)
    store.bootstrap_activate(prior_runtime.resource_id, prior_runtime.installation_id)
    before = store.load()

    with pytest.raises(ReleaseBootstrapError, match="active PHREEQC runtime"):
        bootstrap_release_resources(
            store,
            runtime_manifest=runtime,
            executable_path=executable,
            database_directory=database_dir,
            registry_path=registry_path,
        )

    assert store.load() == before
    assert {item.installation_id for item in store.list_versions(runtime.resource_id)} == {
        prior_runtime.installation_id,
    }


def test_bootstrap_refuses_to_replace_an_active_default_database(
    tmp_path: Path,
) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    store = CatalogStore(tmp_path / "store")
    bootstrap_release_resources(
        store,
        runtime_manifest=runtime,
        executable_path=executable,
        database_directory=database_dir,
        registry_path=registry_path,
    )
    current_database = store.get_active("test.usgs.database.phreeqc")
    assert current_database is not None
    prior_hash = "e" * 64
    prior_path = tmp_path / "prior-phreeqc.dat"
    prior_path.write_bytes(b"reviewed prior database fixture")
    prior_database = replace(
        current_database,
        installation_id=make_installation_id(
            current_database.resource_id, "3.8.5", prior_hash),
        discovered_version="3.8.5",
        installed_version="3.8.5",
        database_sha256=prior_hash,
        content_sha256=prior_hash,
        install_path=str(prior_path.resolve()),
        active_version="",
        superseded_by="",
        rollback_state=RollbackState.CANDIDATE,
    )
    store.register(prior_database)
    store.activate(
        prior_database.resource_id,
        prior_database.installation_id,
        proposal_id="reviewed-test-downgrade",
    )
    before = store.load()

    with pytest.raises(ReleaseBootstrapError, match="active default PHREEQC database"):
        bootstrap_release_resources(
            store,
            runtime_manifest=runtime,
            executable_path=executable,
            database_directory=database_dir,
            registry_path=registry_path,
        )

    assert store.load() == before
    assert store.get_active(prior_database.resource_id).installation_id \
        == prior_database.installation_id


def test_bootstrap_refuses_to_replace_active_knowledge_pack_or_alias(
    tmp_path: Path,
) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    store = CatalogStore(tmp_path / "store")
    bootstrap_release_resources(
        store,
        runtime_manifest=runtime,
        executable_path=executable,
        database_directory=database_dir,
        registry_path=registry_path,
    )
    alias_path = store.knowledge_dir / "knowledge_pack.json"
    alias_before = alias_path.read_bytes()
    state_before = store.load()
    active_before = store.get_active("wpi.phreeqc.knowledge-pack")
    assert active_before is not None

    notes_path = tmp_path / "PHREEQC_RELEASE_NOTES_3.8.6-17100.json"
    notes = json.loads(notes_path.read_text(encoding="utf-8"))
    notes["release_metadata"].append(
        "Versioned fixture metadata changed after knowledge-pack activation.")
    notes_path.write_text(json.dumps(notes, sort_keys=True), encoding="utf-8")

    with pytest.raises(ReleaseBootstrapError, match="active scientific knowledge pack"):
        bootstrap_release_resources(
            store,
            runtime_manifest=runtime,
            executable_path=executable,
            database_directory=database_dir,
            registry_path=registry_path,
        )

    assert store.load() == state_before
    assert alias_path.read_bytes() == alias_before
    assert store.get_active("wpi.phreeqc.knowledge-pack") == active_before


def test_bootstrap_rejects_release_notes_not_bound_to_registry_before_mutation(
    tmp_path: Path,
) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    notes_path = tmp_path / "PHREEQC_RELEASE_NOTES_3.8.6-17100.json"
    notes = json.loads(notes_path.read_text(encoding="utf-8"))
    notes["source_archive_url"] = "https://water.usgs.gov/phreeqc/different.tar.gz"
    notes_path.write_text(json.dumps(notes, sort_keys=True), encoding="utf-8")
    store = CatalogStore(tmp_path / "store")
    with pytest.raises(ReleaseBootstrapError, match="does not match registry source_archive_url"):
        bootstrap_release_resources(
            store,
            runtime_manifest=runtime,
            executable_path=executable,
            database_directory=database_dir,
            registry_path=registry_path,
        )
    assert store.load().generation == 0


def test_bootstrap_missing_database_fails_before_catalog_mutation(tmp_path: Path) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    (database_dir / "Concrete_PHR.dat").unlink()
    store = CatalogStore(tmp_path / "store")
    with pytest.raises(ReleaseBootstrapError, match="missing=.*Concrete_PHR"):
        bootstrap_release_resources(
            store,
            runtime_manifest=runtime,
            executable_path=executable,
            database_directory=database_dir,
            registry_path=registry_path,
        )
    assert store.load().generation == 0


def test_bootstrap_same_filename_with_modified_hash_fails_closed(tmp_path: Path) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    (database_dir / "phreeqc.dat").write_bytes(PHREEQC_DAT + b"# changed\n")
    store = CatalogStore(tmp_path / "store")
    with pytest.raises(ReleaseBootstrapError, match="metadata mismatch.*database_sha256"):
        bootstrap_release_resources(
            store,
            runtime_manifest=runtime,
            executable_path=executable,
            database_directory=database_dir,
            registry_path=registry_path,
        )
    assert store.load().resources == ()


def test_bootstrap_rejects_database_symlink(tmp_path: Path) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    real = tmp_path / "real-phreeqc.dat"
    (database_dir / "phreeqc.dat").replace(real)
    (database_dir / "phreeqc.dat").symlink_to(real)
    with pytest.raises(ReleaseBootstrapError, match="non-symlink"):
        bootstrap_release_resources(
            CatalogStore(tmp_path / "store"),
            runtime_manifest=runtime,
            executable_path=executable,
            database_directory=database_dir,
            registry_path=registry_path,
        )


def test_bootstrap_release_cli_uses_same_verified_api(tmp_path: Path, capsys) -> None:
    executable, database_dir, registry_path, runtime = _release_fixture(tmp_path)
    runtime_path = tmp_path / "runtime-manifest.json"
    runtime_path.write_text(json.dumps(runtime.to_dict()), encoding="utf-8")
    store_root = tmp_path / "cli-store"
    code = steward_main([
        "--store-root", str(store_root),
        "bootstrap-release",
        "--runtime-manifest", str(runtime_path),
        "--executable", str(executable),
        "--database-dir", str(database_dir),
        "--registry", str(registry_path),
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["schema"] == "wpi.virtual-lab.release-resource-bootstrap-result"
    assert not (store_root / "proposals").exists()
