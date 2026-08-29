from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from flyash_phreeqc_ml.resources import (
    ActiveResourceError,
    CatalogError,
    CatalogStore,
    ReferencedResourceError,
    RedistributionState,
    ResourceContractError,
    ResourceKind,
    ResourceManifest,
    RollbackState,
    TestStatus as ResourceTestStatus,
)
from flyash_phreeqc_ml.resources.sources import (
    OfficialSourcePolicy,
    SourcePolicyError,
    inspect_archive,
)


def _manifest(tmp_path: Path, version: str, marker: str) -> ResourceManifest:
    digest = marker * 64
    location = tmp_path / f"runtime-{version}"
    location.write_text(version, encoding="utf-8")
    return ResourceManifest(
        resource_id="wpi.phreeqc.runtime",
        installation_id=f"wpi.phreeqc.runtime-{version}-{digest[:12]}",
        resource_kind=ResourceKind.PHREEQC_RUNTIME,
        display_name="PHREEQC runtime",
        provider="USGS",
        installed_version=version,
        source_sha256=digest,
        executable_sha256=digest,
        content_sha256=digest,
        install_path=str(location.resolve()),
        rights_notice="USGS distribution notice reviewed",
        redistribution_state=RedistributionState.PERMITTED,
        test_status=ResourceTestStatus.PASSED,
        rollback_state=RollbackState.CANDIDATE,
    )


def test_manifest_is_closed_versioned_and_round_trips(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "3.8.6", "a")
    document = manifest.to_dict()
    assert document["manifest_version"] == 1
    assert document["redistribution_state"] == "permitted"
    assert ResourceManifest.from_dict(document) == manifest

    document["unreviewed_field"] = True
    with pytest.raises(ResourceContractError, match="unsupported fields"):
        ResourceManifest.from_dict(document)


def test_catalog_keeps_versions_side_by_side_and_rolls_back_atomically(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "store")
    old = _manifest(tmp_path, "3.8.5", "a")
    new = _manifest(tmp_path, "3.8.6", "b")
    store.register(old)
    store.register(new)
    store.activate(old.resource_id, old.installation_id, proposal_id="initial-load")
    store.activate(new.resource_id, new.installation_id, proposal_id="proposal-upgrade")

    assert store.get_active(old.resource_id).installation_id == new.installation_id
    assert {item.installation_id for item in store.list_versions(old.resource_id)} == {
        old.installation_id, new.installation_id,
    }
    with pytest.raises(ActiveResourceError):
        store.remove_installation(new.installation_id)

    store.rollback_to(old.resource_id, old.installation_id, proposal_id="proposal-upgrade")
    assert store.get_active(old.resource_id).installation_id == old.installation_id
    assert store.get_installation(new.installation_id).rollback_state in {
        RollbackState.ROLLED_BACK, RollbackState.ROLLBACK_AVAILABLE,
    }


def test_bootstrap_activation_is_idempotent_but_cannot_replace_active_resource(
    tmp_path: Path,
) -> None:
    store = CatalogStore(tmp_path / "store")
    old = _manifest(tmp_path, "3.8.5", "a")
    new = _manifest(tmp_path, "3.8.6", "b")
    store.register_many((old, new))
    activated = store.bootstrap_activate(old.resource_id, old.installation_id)

    assert store.bootstrap_activate(old.resource_id, old.installation_id) == activated
    with pytest.raises(CatalogError, match="cannot replace an active installation"):
        store.bootstrap_activate(new.resource_id, new.installation_id)

    assert store.load() == activated
    assert store.get_active(old.resource_id).installation_id == old.installation_id


def test_referenced_resource_cannot_be_deleted(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "store")
    old = _manifest(tmp_path, "3.8.5", "a")
    new = _manifest(tmp_path, "3.8.6", "b")
    store.register(old)
    store.register(new)
    store.activate(old.resource_id, new.installation_id)
    store.record_reference(old.installation_id, "run-0001")

    with pytest.raises(ReferencedResourceError, match="RunRecords"):
        store.remove_installation(old.installation_id)
    retained = store.get_installation(old.installation_id)
    assert retained.installation_id == old.installation_id
    assert retained.primary_sha256 == old.primary_sha256


@pytest.mark.parametrize("url", [
    "http://water.usgs.gov/phreeqc.zip",
    "https://water.usgs.gov.evil.example/phreeqc.zip",
    "https://user:secret@water.usgs.gov/phreeqc.zip",
    "https://example.org/phreeqc.zip",
])
def test_official_source_allowlist_fails_closed(url: str) -> None:
    with pytest.raises(SourcePolicyError):
        OfficialSourcePolicy().validate(url)


def test_redirect_chain_revalidates_every_destination() -> None:
    policy = OfficialSourcePolicy()
    with pytest.raises(SourcePolicyError, match="not allowlisted"):
        policy.validate_redirect_chain((
            "https://water.usgs.gov/phreeqc/source.tar.gz",
            "https://downloads.evil.example/source.tar.gz",
        ))


def test_archive_inspection_rejects_traversal_and_symlinks(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../outside.dat", "PHASES\nCalcite\n")
    with pytest.raises(SourcePolicyError, match="unsafe archive member"):
        inspect_archive(traversal)

    linked = tmp_path / "linked.zip"
    info = zipfile.ZipInfo("database.dat")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(linked, "w") as archive:
        archive.writestr(info, "target.dat")
    with pytest.raises(SourcePolicyError, match="symlink"):
        inspect_archive(linked)


def test_tracked_catalog_and_allowlist_are_closed_json() -> None:
    root = Path(__file__).parents[1] / "resources"
    catalog = json.loads((root / "resource_catalog.json").read_text(encoding="utf-8"))
    allowlist = json.loads(
        (root / "official_source_allowlist.json").read_text(encoding="utf-8"))
    assert set(catalog) == {
        "schema", "version", "generation", "resources", "active", "references",
        "activation_history",
    }
    assert allowlist["policy"]["validate_every_redirect"] is True
    assert tuple(allowlist["hosts"]) == OfficialSourcePolicy().hosts
