"""Official-source policy, bounded downloads, and hostile-archive inspection."""
from __future__ import annotations

import hashlib
import os
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_OFFICIAL_HOSTS = (
    "cemdata.info",
    "empa.ch",
    "pubs.usgs.gov",
    "water.usgs.gov",
    "www.cemdata.info",
    "www.empa.ch",
    "www.usgs.gov",
)
DEFAULT_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_MEMBERS = 20_000
DEFAULT_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024


class SourcePolicyError(ValueError):
    """A URL, redirect, download, or archive violates the official-source policy."""


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    size_bytes: int
    is_directory: bool = False


class OfficialSourcePolicy:
    """Exact HTTPS-host allowlist checked before and after every redirect."""

    def __init__(self, hosts: Iterable[str] = DEFAULT_OFFICIAL_HOSTS):
        normalized = tuple(sorted({str(host).strip().lower().rstrip(".") for host in hosts
                                   if str(host).strip()}))
        if not normalized:
            raise SourcePolicyError("at least one official host is required")
        self.hosts = normalized

    def validate(self, url: str) -> str:
        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https":
            raise SourcePolicyError("official scientific sources must use HTTPS")
        if not host or host not in self.hosts:
            raise SourcePolicyError(f"source host is not allowlisted: {host or '(missing)'}")
        if parts.username or parts.password:
            raise SourcePolicyError("source URLs must not contain credentials")
        try:
            port = parts.port
        except ValueError as exc:
            raise SourcePolicyError("source URL has an invalid port") from exc
        if port not in (None, 443):
            raise SourcePolicyError("official source URLs may use only the HTTPS default port")
        if parts.fragment:
            raise SourcePolicyError("download source URLs must not contain fragments")
        return str(url)

    def validate_redirect_chain(self, urls: Iterable[str]) -> tuple[str, ...]:
        result = tuple(self.validate(url) for url in urls)
        if not result:
            raise SourcePolicyError("redirect chain is empty")
        return result


class _PolicyRedirectHandler(HTTPRedirectHandler):
    def __init__(self, policy: OfficialSourcePolicy):
        super().__init__()
        self.policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        absolute = urljoin(req.full_url, newurl)
        self.policy.validate(absolute)
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def download_official_bytes(url: str, *, policy: OfficialSourcePolicy | None = None,
                            max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
                            timeout: float = 30.0) -> bytes:
    """Download a bounded official resource while validating the final redirect target."""
    policy = policy or OfficialSourcePolicy()
    policy.validate(url)
    opener = build_opener(_PolicyRedirectHandler(policy))
    request = Request(url, headers={"User-Agent": "WPI-Virtual-LAB-resource-steward/1"})
    try:
        with opener.open(request, timeout=timeout) as response:
            policy.validate(response.geturl())
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise SourcePolicyError("official download exceeds the configured size cap")
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SourcePolicyError("official download exceeds the configured size cap")
                chunks.append(chunk)
            return b"".join(chunks)
    except SourcePolicyError:
        raise
    except Exception as exc:  # noqa: BLE001 - converted to a bounded steward error
        raise SourcePolicyError(f"official download failed: {type(exc).__name__}: {exc}") from exc


def _regular_non_symlink(path: Path) -> Path:
    path = Path(path).expanduser()
    try:
        details = path.lstat()
    except OSError as exc:
        raise SourcePolicyError(f"cannot inspect source file: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise SourcePolicyError("source must be a regular non-symlink file")
    return path.resolve(strict=True)


def _safe_member_name(name: str) -> str:
    if "\\" in str(name):
        raise SourcePolicyError(f"unsafe archive member path: {name!r}")
    normalized = str(name)
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or path.is_absolute() \
            or any(part in {"", ".", ".."} for part in path.parts):
        raise SourcePolicyError(f"unsafe archive member path: {name!r}")
    # Windows drive/UNC-like names must not become safe merely because POSIX parsing was used.
    if ":" in path.parts[0] or normalized.startswith("//"):
        raise SourcePolicyError(f"unsafe archive member path: {name!r}")
    return str(path)


def _validate_member_inventory(members: list[ArchiveMember], *, max_members: int,
                               max_expanded_bytes: int) -> tuple[ArchiveMember, ...]:
    if len(members) > max_members:
        raise SourcePolicyError("archive contains too many members")
    names = [item.name for item in members]
    if len(names) != len(set(names)):
        raise SourcePolicyError("archive contains duplicate normalized paths")
    if sum(item.size_bytes for item in members if not item.is_directory) > max_expanded_bytes:
        raise SourcePolicyError("archive expanded contents exceed the configured size cap")
    return tuple(members)


def inspect_archive(path: str | Path, *, require_archive: bool = True,
                    max_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
                    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES) \
        -> tuple[ArchiveMember, ...]:
    """Inspect ZIP/TAR metadata without extraction; reject traversal, links, and devices."""
    source = _regular_non_symlink(Path(path))
    if zipfile.is_zipfile(source):
        members: list[ArchiveMember] = []
        try:
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    name = _safe_member_name(info.filename.rstrip("/") if info.is_dir()
                                             else info.filename)
                    unix_mode = (info.external_attr >> 16) & 0xFFFF
                    if unix_mode and stat.S_ISLNK(unix_mode):
                        raise SourcePolicyError(f"archive symlink is prohibited: {name}")
                    file_type = stat.S_IFMT(unix_mode)
                    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise SourcePolicyError(
                            f"archive special-file member is prohibited: {name}")
                    if info.file_size < 0:
                        raise SourcePolicyError(f"archive member has invalid size: {name}")
                    members.append(ArchiveMember(name, int(info.file_size), info.is_dir()))
        except (OSError, zipfile.BadZipFile) as exc:
            raise SourcePolicyError(f"cannot inspect ZIP archive: {exc}") from exc
        return _validate_member_inventory(
            members, max_members=max_members, max_expanded_bytes=max_expanded_bytes)

    try:
        is_tar = tarfile.is_tarfile(source)
    except OSError:
        is_tar = False
    if is_tar:
        members = []
        try:
            with tarfile.open(source, mode="r:*") as archive:
                for info in archive.getmembers():
                    name = _safe_member_name(info.name.rstrip("/") if info.isdir() else info.name)
                    if info.issym() or info.islnk() or info.isdev() or info.isfifo():
                        raise SourcePolicyError(f"archive link/device is prohibited: {name}")
                    if not (info.isfile() or info.isdir()):
                        raise SourcePolicyError(f"unsupported archive member type: {name}")
                    members.append(ArchiveMember(name, int(info.size), info.isdir()))
        except (OSError, tarfile.TarError) as exc:
            raise SourcePolicyError(f"cannot inspect TAR archive: {exc}") from exc
        return _validate_member_inventory(
            members, max_members=max_members, max_expanded_bytes=max_expanded_bytes)

    if require_archive:
        raise SourcePolicyError("candidate is not a supported ZIP/TAR archive")
    return ()


def archive_database_members(path: str | Path) -> tuple[str, ...]:
    return tuple(sorted(item.name for item in inspect_archive(path)
                        if not item.is_directory and item.name.lower().endswith(".dat")))


def read_archive_member(path: str | Path, member_name: str, *,
                        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES) -> bytes:
    """Read one already-validated regular member without materializing archive paths."""
    source = _regular_non_symlink(Path(path))
    inventory = inspect_archive(source)
    normalized = _safe_member_name(member_name)
    matches = [item for item in inventory if item.name == normalized and not item.is_directory]
    if len(matches) != 1:
        raise SourcePolicyError("selected archive member is absent or ambiguous")
    if matches[0].size_bytes > max_bytes:
        raise SourcePolicyError("selected archive member exceeds the configured size cap")
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            # The normalized name is identical to the original after inventory validation.
            with archive.open(normalized, "r") as handle:
                data = handle.read(max_bytes + 1)
    else:
        with tarfile.open(source, mode="r:*") as archive:
            info = next((item for item in archive.getmembers()
                         if _safe_member_name(item.name) == normalized), None)
            handle = archive.extractfile(info) if info is not None else None
            if handle is None:
                raise SourcePolicyError("selected TAR member cannot be read")
            with handle:
                data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SourcePolicyError("selected archive member exceeds the configured size cap")
    return data


def hash_archive_members(path: str | Path, *,
                         max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES) \
        -> dict[str, str]:
    """Hash every regular member through archive streams without extracting to disk."""
    source = _regular_non_symlink(Path(path))
    inventory = inspect_archive(source, max_expanded_bytes=max_expanded_bytes)
    expected = {item.name: item.size_bytes for item in inventory if not item.is_directory}
    result: dict[str, str] = {}
    total = 0

    def consume(handle, name: str) -> str:
        nonlocal total
        digest = hashlib.sha256()
        member_total = 0
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            member_total += len(chunk)
            total += len(chunk)
            if member_total > expected[name] or total > max_expanded_bytes:
                raise SourcePolicyError("archive contents exceed their reviewed size bounds")
            digest.update(chunk)
        if member_total != expected[name]:
            raise SourcePolicyError(f"archive member size changed during hashing: {name}")
        return digest.hexdigest()

    try:
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                by_name = {
                    _safe_member_name(info.filename): info for info in archive.infolist()
                    if not info.is_dir()
                }
                for name in sorted(expected):
                    with archive.open(by_name[name], "r") as handle:
                        result[name] = consume(handle, name)
        else:
            with tarfile.open(source, mode="r:*") as archive:
                by_name = {
                    _safe_member_name(info.name): info for info in archive.getmembers()
                    if info.isfile()
                }
                for name in sorted(expected):
                    handle = archive.extractfile(by_name[name])
                    if handle is None:
                        raise SourcePolicyError(f"archive member cannot be hashed: {name}")
                    with handle:
                        result[name] = consume(handle, name)
    except SourcePolicyError:
        raise
    except (OSError, KeyError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise SourcePolicyError(f"cannot hash archive contents safely: {exc}") from exc
    return dict(sorted(result.items()))


DownloadFetcher = Callable[[str], bytes]
