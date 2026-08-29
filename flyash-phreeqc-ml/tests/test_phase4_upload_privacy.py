"""Phase 4 upload limits and privacy-safe diagnostic tests."""
from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from flyash_phreeqc_ml import audit, config, run_manager
from flyash_phreeqc_ml.security.identity import (
    DeploymentMode,
    IdentityContext,
    ROLE_MEMBER,
    identity_context,
)
from flyash_phreeqc_ml.upload_guard import (
    UploadPolicy,
    UploadValidationError,
    validate_table_shape,
    validate_upload,
)


class _BoundedStream(io.BytesIO):
    name = "private-results.csv"

    @property
    def size(self):
        return len(self.getbuffer())


def _xlsx(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return out.getvalue()


def _tar(entries: dict[str, bytes], *, compressed: bool = False) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz" if compressed else "w") as archive:
        for name, value in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return out.getvalue()


def test_upload_size_is_refused_before_reading_content():
    stream = _BoundedStream(b"secret-data" * 100)
    with pytest.raises(UploadValidationError) as caught:
        validate_upload(stream, policy=UploadPolicy(max_bytes=16))
    assert caught.value.code == "too_large"
    assert stream.tell() == 0
    assert "secret-data" not in str(caught.value)


def test_csv_row_and_column_limits_are_bounded():
    with pytest.raises(UploadValidationError) as rows:
        validate_upload(b"a,b\n1,2\n3,4\n", filename="x.csv",
                        policy=UploadPolicy(max_rows=2))
    assert rows.value.code == "too_many_rows"
    with pytest.raises(UploadValidationError) as columns:
        validate_upload(b"a,b,c\n", filename="x.csv",
                        policy=UploadPolicy(max_columns=2))
    assert columns.value.code == "too_many_columns"


def test_xlsx_archive_traversal_and_compression_bomb_are_refused():
    traversal = _xlsx({
        "[Content_Types].xml": b"<Types/>",
        "xl/workbook.xml": b"<workbook/>",
        "../escape": b"bad",
    })
    with pytest.raises(UploadValidationError) as unsafe:
        validate_upload(traversal, filename="x.xlsx")
    assert unsafe.value.code == "archive_path"

    bomb = _xlsx({
        "[Content_Types].xml": b"<Types/>",
        "xl/workbook.xml": b"<workbook/>",
        "xl/worksheets/sheet1.xml": b"A" * 100_000,
    })
    with pytest.raises(UploadValidationError) as ratio:
        validate_upload(bomb, filename="x.xlsx",
                        policy=UploadPolicy(max_compression_ratio=10))
    assert ratio.value.code == "compression_ratio"


def test_database_archives_are_bounded_without_extraction():
    database = b"SOLUTION_MASTER_SPECIES\nH H+ 0.0 H 1.0\n"
    for name, payload in (
        ("reviewed.zip", _xlsx({"nested/cemdata.dat": database})),
        ("reviewed.tar", _tar({"nested/cemdata.dat": database})),
        ("reviewed.tar.gz", _tar({"nested/cemdata.dat": database}, compressed=True)),
    ):
        result = validate_upload(
            payload, filename=name,
            allowed_extensions={".zip", ".tar", ".tar.gz", ".tgz"},
        )
        assert result.extension in {".zip", ".tar", ".tar.gz"}
        assert result.data == payload


def test_database_archives_refuse_traversal_links_and_compression_bombs():
    traversal = _xlsx({"../outside.dat": b"SOLUTION 1\nEND\n"})
    with pytest.raises(UploadValidationError) as path_error:
        validate_upload(traversal, filename="reviewed.zip",
                        allowed_extensions={".zip"})
    assert path_error.value.code == "archive_path"

    windows_path = _xlsx({"C:/outside.dat": b"SOLUTION 1\nEND\n"})
    with pytest.raises(UploadValidationError) as windows_path_error:
        validate_upload(windows_path, filename="reviewed.zip",
                        allowed_extensions={".zip"})
    assert windows_path_error.value.code == "archive_path"

    link_out = io.BytesIO()
    with tarfile.open(fileobj=link_out, mode="w") as archive:
        info = tarfile.TarInfo("linked.dat")
        info.type = tarfile.SYMTYPE
        info.linkname = "../outside.dat"
        archive.addfile(info)
    with pytest.raises(UploadValidationError) as link_error:
        validate_upload(link_out.getvalue(), filename="reviewed.tar",
                        allowed_extensions={".tar"})
    assert link_error.value.code == "archive_link"

    bomb = _tar({"large.dat": b"A" * 250_000}, compressed=True)
    with pytest.raises(UploadValidationError) as ratio_error:
        validate_upload(
            bomb, filename="reviewed.tar.gz",
            policy=UploadPolicy(max_compression_ratio=10),
            allowed_extensions={".tar.gz"},
        )
    assert ratio_error.value.code == "compression_ratio"


def test_xml_external_entities_and_mismatched_spreadsheet_magic_are_refused():
    with pytest.raises(UploadValidationError) as xml:
        validate_upload(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>',
                        filename="pattern.xrdml")
    assert xml.value.code == "unsafe_xml"
    with pytest.raises(UploadValidationError) as workbook:
        validate_upload(b"not-a-workbook", filename="data.xlsx")
    assert workbook.value.code == "type_mismatch"


def test_validated_upload_safe_surface_omits_name_and_contents():
    secret = b"sample_id,Ca_mM\nprivate-001,1.2\n"
    result = validate_upload(secret, filename="subject-private-results.csv")
    safe = result.to_safe_dict()
    assert result.data == secret
    assert "subject-private-results" not in str(safe)
    assert "private-001" not in str(safe)
    assert "private-001" not in repr(result)
    assert set(safe) == {"extension", "size_bytes", "sha256", "validated"}


def test_post_parse_table_shape_guard():
    class Table:
        shape = (101, 3)

    with pytest.raises(UploadValidationError) as caught:
        validate_table_shape(Table(), policy=UploadPolicy(max_rows=100))
    assert caught.value.code == "too_many_rows"


def test_json_nesting_and_node_limits_are_bounded():
    nested = "0"
    for _ in range(70):
        nested = f"[{nested}]"
    with pytest.raises(UploadValidationError) as depth:
        validate_upload(nested.encode(), filename="reference.json")
    assert depth.value.code in {"json_depth", "malformed_json"}

    with pytest.raises(UploadValidationError) as nodes:
        validate_upload(json.dumps(list(range(20))).encode(), filename="reference.json",
                        policy=UploadPolicy(max_rows=5))
    assert nodes.value.code == "too_many_rows"


def _hosted() -> IdentityContext:
    return IdentityContext("private-user", "private-tenant", frozenset({ROLE_MEMBER}),
                           DeploymentMode.HOSTED)


def test_audit_schema_redacts_prompts_secrets_endpoints_and_uses_opaque_actor_scope(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    with identity_context(_hosted()):
        run_manager.create_run("audit", "lab_experiment")
        assert audit.log_event("audit", "custom_event", {
            "api_key": "sk-private-secret",
            "endpoint": "http://private.internal/v1",
            "prompt": "private research prompt",
            "nested": {"authorization": "Bearer private-token"},
            "count": 3,
        })
        assert audit.log_event("audit", "custom_event", {"count": 4})
        path = audit.audit_log_path("audit")
        raw = path.read_text(encoding="utf-8")
    assert "private-user" not in raw and "private-tenant" not in raw
    assert "sk-private-secret" not in raw and "private.internal" not in raw
    assert "private research prompt" not in raw and "private-token" not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    assert records[0]["schema_version"] == audit.AUDIT_SCHEMA_VERSION
    assert records[0]["actor_scope"] == records[1]["actor_scope"]
    assert len(records[0]["actor_scope"]) == 64
    assert records[0]["payload"]["count"] == 3


def test_audit_append_enforces_total_file_cap_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    run_manager.create_run("audit-cap", "lab_experiment")
    assert audit.log_event("audit-cap", "custom_event", {"count": 1})
    path = audit.audit_log_path("audit-cap")
    before = path.read_bytes()
    monkeypatch.setattr(audit, "MAX_AUDIT_FILE_BYTES", len(before))
    with pytest.warns(UserWarning, match="could not be recorded"):
        assert audit.log_event("audit-cap", "custom_event", {"count": 2}) is False
    assert path.read_bytes() == before


def test_every_research_uploader_invokes_central_guard_and_avoids_direct_reads():
    root = Path(__file__).resolve().parents[1] / "ui"
    uploader_files = {
        "import_tab.py", "phase3_icp.py", "phase3_xrd.py", "simulate_tab.py", "settings.py",
    }
    for name in uploader_files:
        source = (root / name).read_text(encoding="utf-8")
        assert "file_uploader" in source
        assert "_guard_uploaded_file" in source or "validate_upload(" in source
        assert ".getvalue()" not in source


def test_database_manager_selects_one_archive_member_and_never_extracts_or_concatenates():
    source = (Path(__file__).resolve().parents[1] / "ui" / "settings.py").read_text(
        encoding="utf-8")
    assert "archive_database_members" in source
    assert "archive_member=archive_member" in source
    assert '".zip", ".tar", ".tar.gz", ".tgz"' in source
    assert "extractall(" not in source
    assert "concatenate(" not in source
