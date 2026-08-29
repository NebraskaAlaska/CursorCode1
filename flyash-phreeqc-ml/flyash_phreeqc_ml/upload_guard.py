"""Central, privacy-safe validation for browser uploads.

The guard validates size before consuming the full stream, constrains spreadsheet
archives before any parser opens them, and never returns file contents in errors or
diagnostics.  Domain importers remain responsible for scientific schema/unit checks.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


DEFAULT_MAX_UPLOAD_BYTES = _positive_env("VLAB_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
DEFAULT_MAX_ARCHIVE_BYTES = _positive_env("VLAB_MAX_ARCHIVE_EXPANDED_BYTES", 100 * 1024 * 1024)

_SAFE_FILENAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")
_TEXT_EXTENSIONS = frozenset({".csv", ".tsv", ".txt", ".xy", ".xye", ".dat", ".json"})
_XML_EXTENSIONS = frozenset({".xml", ".xrdml"})
_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".xlsm", ".xls"})
_ARCHIVE_EXTENSIONS = frozenset({".zip", ".tar", ".tar.gz", ".tgz"})


class UploadValidationError(ValueError):
    """Controlled upload refusal with a stable, content-free category."""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(str(message))


@dataclass(frozen=True)
class UploadPolicy:
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_rows: int = 250_000
    max_columns: int = 2_000
    max_archive_entries: int = 2_048
    max_archive_expanded_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    max_compression_ratio: float = 100.0
    allowed_extensions: frozenset[str] = field(default_factory=lambda: frozenset(
        _TEXT_EXTENSIONS | _XML_EXTENSIONS | _SPREADSHEET_EXTENSIONS))

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_rows", "max_columns", "max_archive_entries",
                     "max_archive_expanded_bytes"):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if float(self.max_compression_ratio) <= 1:
            raise ValueError("max_compression_ratio must be greater than 1")
        normalized = frozenset(
            item if str(item).startswith(".") else f".{item}"
            for item in (str(value).strip().lower() for value in self.allowed_extensions)
            if item != ".")
        if not normalized:
            raise ValueError("allowed_extensions must not be empty")
        object.__setattr__(self, "allowed_extensions", normalized)


@dataclass(frozen=True)
class ValidatedUpload:
    """Validated bytes plus content-free metadata.

    ``data`` is deliberately omitted from ``repr`` and :meth:`to_safe_dict`; the
    latter is the only representation suitable for diagnostics or audit logs.
    """

    filename: str
    extension: str
    size_bytes: int
    sha256: str
    data: bytes = field(repr=False)

    def to_safe_dict(self) -> dict:
        return {
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "validated": True,
        }


def _filename(upload: object, supplied: str | None) -> str:
    raw = supplied if supplied is not None else getattr(upload, "name", "")
    name = str(raw or "").strip()
    if not _SAFE_FILENAME_RE.fullmatch(name) or Path(name).name != name \
            or "/" in name or "\\" in name or name in {".", ".."}:
        raise UploadValidationError("unsafe_filename", "The upload filename is not safe.")
    return name


def _extension(name: str) -> str:
    lowered = name.lower()
    return ".tar.gz" if lowered.endswith(".tar.gz") else Path(lowered).suffix


def _read_bounded(upload: object, max_bytes: int) -> bytes:
    if isinstance(upload, (bytes, bytearray, memoryview)):
        data = bytes(upload)
        if len(data) > max_bytes:
            raise UploadValidationError("too_large", "The upload exceeds the configured size limit.")
        return data

    declared = getattr(upload, "size", None)
    if isinstance(declared, int) and declared > max_bytes:
        raise UploadValidationError("too_large", "The upload exceeds the configured size limit.")
    reader = getattr(upload, "read", None)
    if not callable(reader):
        # Compatibility for Streamlit-style/test upload objects that expose only
        # ``getvalue``. The returned buffer is still size-checked before use.
        getter = getattr(upload, "getvalue", None)
        if not callable(getter):
            raise UploadValidationError("unreadable", "The upload could not be read safely.")
        try:
            chunk = getter()
        except Exception as exc:
            raise UploadValidationError("unreadable", "The upload could not be read safely.") from exc
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise UploadValidationError("unreadable", "The upload did not provide byte content.")
        data = bytes(chunk)
        if len(data) > max_bytes:
            raise UploadValidationError("too_large", "The upload exceeds the configured size limit.")
        return data
    position = None
    try:
        position = upload.tell()
    except Exception:
        pass
    try:
        chunk = reader(max_bytes + 1)
    except Exception as exc:
        raise UploadValidationError("unreadable", "The upload could not be read safely.") from exc
    finally:
        if position is not None:
            try:
                upload.seek(position)
            except Exception:
                pass
    if isinstance(chunk, str):
        chunk = chunk.encode("utf-8")
    if not isinstance(chunk, (bytes, bytearray, memoryview)):
        raise UploadValidationError("unreadable", "The upload did not provide byte content.")
    data = bytes(chunk)
    if len(data) > max_bytes:
        raise UploadValidationError("too_large", "The upload exceeds the configured size limit.")
    return data


def _validate_text(data: bytes, extension: str, policy: UploadPolicy) -> None:
    if b"\x00" in data:
        raise UploadValidationError("binary_mismatch", "A text upload contains binary data.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadValidationError("encoding", "Text uploads must use UTF-8 encoding.") from exc
    if extension == ".json":
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise UploadValidationError("malformed_json", "The JSON upload is malformed.") from exc
        nodes = 0
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > policy.max_rows * 4:
                raise UploadValidationError("json_nodes", "The JSON upload is too complex.")
            if depth > 64:
                raise UploadValidationError("json_depth", "The JSON nesting depth is unsafe.")
            if isinstance(item, dict):
                if len(item) > policy.max_columns:
                    raise UploadValidationError(
                        "too_many_columns", "A JSON object exceeds the field limit.")
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                if len(item) > policy.max_rows:
                    raise UploadValidationError("too_many_rows", "A JSON list exceeds the row limit.")
                stack.extend((child, depth + 1) for child in item)
        return
    if extension not in {".csv", ".tsv"}:
        return
    delimiter = "\t" if extension == ".tsv" else ","
    rows = 0
    try:
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter):
            rows += 1
            if rows > policy.max_rows:
                raise UploadValidationError("too_many_rows", "The table exceeds the row limit.")
            if len(row) > policy.max_columns:
                raise UploadValidationError(
                    "too_many_columns", "The table exceeds the column limit.")
    except csv.Error as exc:
        raise UploadValidationError("malformed_table", "The delimited table is malformed.") from exc


def _safe_archive_member(name: str) -> bool:
    raw = str(name or "")
    if not raw or "\\" in raw or raw.startswith("/") or raw.startswith("//") \
            or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return False
    normalized = raw.rstrip("/")
    parts = normalized.split("/")
    pure = PurePosixPath(normalized)
    return bool(normalized) and not pure.is_absolute() \
        and all(part not in {"", ".", ".."} for part in parts) \
        and ":" not in parts[0]


def _validate_xlsx(data: bytes, policy: UploadPolicy) -> None:
    if not data.startswith(b"PK"):
        raise UploadValidationError("type_mismatch", "The spreadsheet signature is invalid.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > policy.max_archive_entries:
                raise UploadValidationError(
                    "archive_entries", "The spreadsheet archive contains too many entries.")
            names = {member.filename for member in members}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise UploadValidationError(
                    "type_mismatch", "The upload is not a valid Excel workbook.")
            expanded = 0
            compressed = 0
            for member in members:
                if not _safe_archive_member(member.filename):
                    raise UploadValidationError(
                        "archive_path", "The spreadsheet archive contains an unsafe path.")
                if member.flag_bits & 0x1:
                    raise UploadValidationError(
                        "encrypted_archive", "Encrypted spreadsheet archives are not accepted.")
                mode = (member.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise UploadValidationError(
                        "archive_link", "Spreadsheet archive links are not accepted.")
                expanded += int(member.file_size)
                compressed += int(member.compress_size)
                if member.file_size > policy.max_archive_expanded_bytes:
                    raise UploadValidationError(
                        "archive_expanded", "A spreadsheet entry exceeds the expansion limit.")
                if member.file_size and member.compress_size == 0:
                    raise UploadValidationError(
                        "compression_ratio", "The spreadsheet compression ratio is unsafe.")
                if member.compress_size and member.file_size / member.compress_size \
                        > policy.max_compression_ratio:
                    raise UploadValidationError(
                        "compression_ratio", "The spreadsheet compression ratio is unsafe.")
            if expanded > policy.max_archive_expanded_bytes:
                raise UploadValidationError(
                    "archive_expanded", "The spreadsheet exceeds the expansion limit.")
            if compressed and expanded / compressed > policy.max_compression_ratio:
                raise UploadValidationError(
                    "compression_ratio", "The spreadsheet compression ratio is unsafe.")
    except UploadValidationError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise UploadValidationError("malformed_archive", "The spreadsheet archive is malformed.") from exc


def _validate_generic_archive(data: bytes, extension: str, policy: UploadPolicy) -> None:
    """Bound ZIP/TAR metadata without extracting any member."""
    if extension == ".zip":
        if not data.startswith(b"PK"):
            raise UploadValidationError("type_mismatch", "The ZIP archive signature is invalid.")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > policy.max_archive_entries:
                    raise UploadValidationError(
                        "archive_entries", "The archive contains too many entries.")
                expanded = compressed = 0
                for member in members:
                    if not _safe_archive_member(member.filename):
                        raise UploadValidationError(
                            "archive_path", "The archive contains an unsafe path.")
                    if member.flag_bits & 0x1:
                        raise UploadValidationError(
                            "encrypted_archive", "Encrypted archives are not accepted.")
                    mode = (member.external_attr >> 16) & 0xFFFF
                    file_type = stat.S_IFMT(mode)
                    if mode and (stat.S_ISLNK(mode) or file_type not in {
                            0, stat.S_IFREG, stat.S_IFDIR}):
                        raise UploadValidationError(
                            "archive_link", "Archive links or special files are not accepted.")
                    expanded += int(member.file_size)
                    compressed += int(member.compress_size)
                    if member.file_size > policy.max_archive_expanded_bytes:
                        raise UploadValidationError(
                            "archive_expanded", "An archive entry exceeds the expansion limit.")
                    if member.file_size and member.compress_size == 0:
                        raise UploadValidationError(
                            "compression_ratio", "The archive compression ratio is unsafe.")
                    if member.compress_size and member.file_size / member.compress_size \
                            > policy.max_compression_ratio:
                        raise UploadValidationError(
                            "compression_ratio", "The archive compression ratio is unsafe.")
                if expanded > policy.max_archive_expanded_bytes:
                    raise UploadValidationError(
                        "archive_expanded", "The archive exceeds the expansion limit.")
                if compressed and expanded / compressed > policy.max_compression_ratio:
                    raise UploadValidationError(
                        "compression_ratio", "The archive compression ratio is unsafe.")
        except UploadValidationError:
            raise
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise UploadValidationError("malformed_archive", "The ZIP archive is malformed.") from exc
        return

    tar_data = data
    if extension in {".tar.gz", ".tgz"}:
        if len(data) < 18 or not data.startswith(b"\x1f\x8b"):
            raise UploadValidationError("type_mismatch", "The gzip archive signature is invalid.")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as compressed:
                tar_data = compressed.read(policy.max_archive_expanded_bytes + 1)
        except (OSError, EOFError) as exc:
            raise UploadValidationError(
                "malformed_archive", "The gzip archive is malformed.") from exc
        if len(tar_data) > policy.max_archive_expanded_bytes:
            raise UploadValidationError("archive_expanded", "The archive exceeds the expansion limit.")
        if tar_data and len(tar_data) / max(1, len(data)) > policy.max_compression_ratio:
            raise UploadValidationError(
                "compression_ratio", "The archive compression ratio is unsafe.")
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > policy.max_archive_entries:
                raise UploadValidationError(
                    "archive_entries", "The archive contains too many entries.")
            expanded = 0
            for member in members:
                if not _safe_archive_member(member.name):
                    raise UploadValidationError(
                        "archive_path", "The archive contains an unsafe path.")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo() \
                        or not (member.isfile() or member.isdir()):
                    raise UploadValidationError(
                        "archive_link", "Archive links or special files are not accepted.")
                expanded += int(member.size)
                if member.size > policy.max_archive_expanded_bytes \
                        or expanded > policy.max_archive_expanded_bytes:
                    raise UploadValidationError(
                        "archive_expanded", "The archive exceeds the expansion limit.")
    except UploadValidationError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise UploadValidationError("malformed_archive", "The TAR archive is malformed.") from exc


def _validate_xml(data: bytes) -> None:
    prefix = data[:4096].lstrip().lower()
    if not (prefix.startswith(b"<?xml") or prefix.startswith(b"<")):
        raise UploadValidationError("type_mismatch", "The XML signature is invalid.")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise UploadValidationError(
            "unsafe_xml", "XML uploads may not declare external document types or entities.")


def validate_upload(
    upload: object,
    *,
    filename: str | None = None,
    policy: UploadPolicy | None = None,
    allowed_extensions: Iterable[str] | None = None,
) -> ValidatedUpload:
    """Read and validate one upload without logging its name or contents."""
    active = policy or UploadPolicy()
    name = _filename(upload, filename)
    extension = _extension(name)
    allowed = active.allowed_extensions
    if allowed_extensions is not None:
        allowed = frozenset(
            value if value.startswith(".") else f".{value}"
            for value in (str(item).strip().lower() for item in allowed_extensions))
    if extension not in allowed:
        raise UploadValidationError("extension", "This upload type is not allowed.")
    data = _read_bounded(upload, active.max_bytes)
    if not data:
        raise UploadValidationError("empty", "The upload is empty.")

    if extension in _TEXT_EXTENSIONS:
        _validate_text(data, extension, active)
    elif extension in {".xlsx", ".xlsm"}:
        _validate_xlsx(data, active)
    elif extension == ".xls":
        if not data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
            raise UploadValidationError("type_mismatch", "The spreadsheet signature is invalid.")
    elif extension in _XML_EXTENSIONS:
        _validate_xml(data)
    elif extension in _ARCHIVE_EXTENSIONS:
        _validate_generic_archive(data, extension, active)

    return ValidatedUpload(
        filename=name,
        extension=extension,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def validate_table_shape(table, *, policy: UploadPolicy | None = None) -> None:
    """Apply row/column limits after a protected parser returns a table object."""
    active = policy or UploadPolicy()
    shape = getattr(table, "shape", None)
    if not isinstance(shape, tuple) or len(shape) < 2:
        raise UploadValidationError("table_shape", "The parsed table has no usable shape.")
    rows, columns = int(shape[0]), int(shape[1])
    if rows > active.max_rows:
        raise UploadValidationError("too_many_rows", "The table exceeds the row limit.")
    if columns > active.max_columns:
        raise UploadValidationError("too_many_columns", "The table exceeds the column limit.")
