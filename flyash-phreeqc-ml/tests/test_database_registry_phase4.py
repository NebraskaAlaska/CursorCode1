from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from flyash_phreeqc_ml.resources import (
    CatalogStore,
    DatabaseRegistry,
    DatabaseRegistryError,
    ExternalDatabaseImporter,
    RedistributionState,
    ResourceKind,
    TestStatus as ResourceTestStatus,
    parse_database_bytes,
)


DATABASE = b"""# version PHREEQC 3.8.6-17100
SOLUTION_MASTER_SPECIES
Ca Ca+2 0 Ca 40.08
SOLUTION_SPECIES
Ca+2 = Ca+2
    log_k 0
PHASES
Calcite
    CaCO3 = Ca+2 + CO3-2
    log_k -8.48
Portlandite
    Ca(OH)2 = Ca+2 + 2OH-
    log_k 22.8
END
# GhostPhase must never be parsed from a comment
"""


def test_section_aware_database_parser_hashes_and_summarizes() -> None:
    summary = parse_database_bytes(DATABASE, filename="phreeqc.dat")
    assert summary.detected_version == "3.8.6-17100"
    assert summary.master_species == ("Ca",)
    assert summary.solution_species == ("Ca+2",)
    assert summary.phases == ("Calcite", "Portlandite")
    assert "GhostPhase" not in summary.phases
    assert len(summary.source_sha256) == 64


@pytest.mark.parametrize(
    ("filename", "base_filename", "base_family"),
    [
        ("Concrete_PHR.dat", "phreeqc.dat", "phreeqc"),
        ("Concrete_PZ.dat", "pitzer.dat", "pitzer"),
    ],
)
def test_official_concrete_files_are_extensions_not_standalone_databases(
    filename: str, base_filename: str, base_family: str,
) -> None:
    raw = (
        f"# INCLUDE$ add-on; for use with {base_filename}\n"
        "PHASES\nCement_phase\nCaO = Ca+2 + O-2\nlog_k 1\n"
    ).encode()
    summary = parse_database_bytes(raw, filename=filename)

    assert summary.is_database_extension is True
    assert summary.requires_database_filename == base_filename
    assert summary.requires_database_family == base_family
    assert summary.standalone_test_status == ResourceTestStatus.NOT_APPLICABLE_REQUIRES_BASE
    assert "explicit reviewed INCLUDE$" in summary.compatibility_warning


def test_import_requires_human_rights_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "external.dat"
    source.write_bytes(DATABASE)
    importer = ExternalDatabaseImporter(CatalogStore(tmp_path / "store"))
    with pytest.raises(DatabaseRegistryError, match="rights confirmation"):
        importer.import_database(
            source,
            resource_id="external.cement.db",
            display_name="External cement DB",
            provider="User-supplied",
            installed_version="1",
            rights_confirmed=False,
            rights_notice="",
            redistribution_state=RedistributionState.UNKNOWN,
            actor_role="user",
        )

    with pytest.raises(DatabaseRegistryError, match="administrator-only"):
        importer.import_database(
            source,
            resource_id="external.cement.db",
            display_name="External cement DB",
            provider="User-supplied",
            installed_version="1",
            rights_confirmed=True,
            rights_notice="User claims local-use rights",
            redistribution_state=RedistributionState.USER_SUPPLIED,
            actor_role="user",
            deployment_scope="hosted",
        )

    with pytest.raises(DatabaseRegistryError, match="prohibited"):
        importer.import_database(
            source,
            resource_id="external.cement.db",
            display_name="External cement DB",
            provider="User-supplied",
            installed_version="1",
            rights_confirmed=True,
            rights_notice="Redistribution is prohibited",
            redistribution_state=RedistributionState.PROHIBITED,
            actor_role="admin",
        )


def test_archive_import_selects_one_database_and_never_concatenates(tmp_path: Path) -> None:
    archive_path = tmp_path / "databases.zip"
    other = DATABASE.replace(b"Calcite", b"Gypsum")
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("one.dat", DATABASE)
        archive.writestr("two.dat", other)
    store = CatalogStore(tmp_path / "store")
    importer = ExternalDatabaseImporter(store)

    with pytest.raises(DatabaseRegistryError, match="explicit member selection"):
        importer.import_database(
            archive_path,
            resource_id="external.cement.db",
            display_name="External cement DB",
            provider="User-supplied",
            installed_version="1",
            rights_confirmed=True,
            rights_notice="User confirmed local use rights",
            redistribution_state=RedistributionState.USER_SUPPLIED,
            actor_role="user",
        )

    manifest = importer.import_database(
        archive_path,
        resource_id="external.cement.db",
        display_name="External cement DB",
        provider="User-supplied",
        installed_version="1",
        rights_confirmed=True,
        rights_notice="User confirmed local use rights",
        redistribution_state=RedistributionState.USER_SUPPLIED,
        actor_role="user",
        archive_member="one.dat",
    )
    assert Path(manifest.install_path).read_bytes() == DATABASE
    assert "Gypsum" not in manifest.supported_summary.phases
    assert manifest.resource_kind == ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE
    assert DatabaseRegistry(store).available_phases(manifest.installation_id) == (
        "Calcite", "Portlandite",
    )


def test_concrete_import_records_extension_dependency_and_no_standalone_test(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Concrete_PZ.dat"
    source.write_text(
        "# For use with pitzer.dat as an INCLUDE$ add-on\n"
        "PHASES\nCement_phase\nCaO = Ca+2 + O-2\nlog_k 1\n",
        encoding="utf-8",
    )
    manifest = ExternalDatabaseImporter(CatalogStore(tmp_path / "store")).import_database(
        source,
        resource_id="usgs.concrete.pz",
        display_name="USGS Concrete PZ extension",
        provider="USGS",
        installed_version="reviewed",
        rights_confirmed=True,
        rights_notice="USGS rights reviewed",
        redistribution_state=RedistributionState.PERMITTED,
        actor_role="admin",
    )
    assert manifest.resource_kind == ResourceKind.DATABASE_EXTENSION
    assert manifest.requires_database_family == "pitzer"
    assert "pitzer.dat" in manifest.dependencies
    assert manifest.test_status == ResourceTestStatus.NOT_APPLICABLE_REQUIRES_BASE
    assert "no files were concatenated" in " ".join(manifest.warnings)
