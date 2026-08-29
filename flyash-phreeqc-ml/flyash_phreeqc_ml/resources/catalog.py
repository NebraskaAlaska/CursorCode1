"""Atomic, side-by-side storage for scientific-resource manifests."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from .models import (
    ActivationRecord,
    CatalogState,
    ResourceContractError,
    ResourceManifest,
    RollbackState,
)

CATALOG_FILE = "catalog.json"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class CatalogError(RuntimeError):
    """The catalog cannot complete a safe state transition."""


class ReferencedResourceError(CatalogError):
    """A run still references the exact installation being removed."""


class ActiveResourceError(CatalogError):
    """An active installation cannot be removed."""


def atomic_write_bytes(path: Path, payload: bytes, *, create_only: bool = False) -> None:
    """Write bytes with fsync + atomic replacement and refuse symlink destinations."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise CatalogError(f"refusing a symlink-backed write: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise CatalogError(f"destination already exists: {path}") from exc
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: Path, document: dict, *, create_only: bool = False) -> None:
    try:
        payload = (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False,
                              allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"catalog document is not strict JSON: {exc}") from exc
    atomic_write_bytes(path, payload, create_only=create_only)


class CatalogStore:
    """One authoritative manifest catalog with side-by-side installation identities.

    Activating a new installation changes only the small catalog pointer.  Existing installed
    directories and manifests remain present, and any installation referenced by a durable run
    cannot be removed from the catalog.
    """

    def __init__(self, root: str | Path):
        raw = Path(root).expanduser()
        if any(part == ".." for part in raw.parts):
            raise CatalogError("catalog root must not contain '..'")
        self.root = raw.absolute()
        if self.root.exists() and self.root.is_symlink():
            raise CatalogError("catalog root must not be a symlink")

    def _path(self, *parts: str) -> Path:
        if self.root.exists() and self.root.is_symlink():
            raise CatalogError("catalog root must not be a symlink")
        if any(not part or part in {".", ".."} or Path(part).name != part
               or "/" in part or "\\" in part for part in parts):
            raise CatalogError("unsafe catalog path component")
        candidate = self.root.joinpath(*parts)
        try:
            candidate.resolve(strict=False).relative_to(self.root.resolve(strict=False))
        except ValueError as exc:
            raise CatalogError("catalog path escapes the resource root") from exc
        current = self.root
        for part in parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise CatalogError(f"symlink path component is not permitted: {current}")
        return candidate

    @property
    def catalog_path(self) -> Path:
        return self._path(CATALOG_FILE)

    @property
    def quarantine_dir(self) -> Path:
        return self._path("quarantine")

    @property
    def proposals_dir(self) -> Path:
        return self._path("proposals")

    @property
    def knowledge_dir(self) -> Path:
        return self._path("knowledge")

    def installation_dir(self, installation_id: str) -> Path:
        # ResourceManifest validation owns the exact identifier grammar.  Reject all path syntax
        # here even before a manifest is available.
        if not installation_id or Path(installation_id).name != installation_id \
                or "/" in installation_id or "\\" in installation_id:
            raise CatalogError("unsafe installation identity")
        return self._path("installed", installation_id)

    @contextmanager
    def locked(self) -> Iterator[None]:
        key = str(self.root)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink():
                raise CatalogError("catalog root must not be a symlink")
            lock_path = self._path("catalog.lock")
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise CatalogError("cannot acquire the catalog lock safely") from exc
            try:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                except ImportError:
                    pass
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except ImportError:
                    pass
                os.close(descriptor)

    def load(self) -> CatalogState:
        path = self.catalog_path
        if path.is_symlink():
            raise CatalogError("catalog file must not be a symlink")
        if not path.exists():
            return CatalogState()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            return CatalogState.from_dict(document)
        except (OSError, json.JSONDecodeError, ResourceContractError) as exc:
            raise CatalogError(f"cannot load the resource catalog: {exc}") from exc

    def _commit(self, current: CatalogState, next_state: CatalogState) -> CatalogState:
        if next_state.generation != current.generation + 1:
            raise CatalogError("catalog generation must advance exactly once")
        history = self._path("history")
        history.mkdir(parents=True, exist_ok=True)
        snapshot = self._path("history", f"catalog-{next_state.generation:08d}.json")
        if snapshot.exists():
            try:
                prior = CatalogState.from_dict(json.loads(snapshot.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ResourceContractError) as exc:
                raise CatalogError("existing catalog history snapshot is invalid") from exc
            if prior.to_dict() != next_state.to_dict():
                raise CatalogError("catalog generation already has different immutable evidence")
        else:
            atomic_write_json(snapshot, next_state.to_dict(), create_only=True)
        atomic_write_json(self.catalog_path, next_state.to_dict())
        return next_state

    def register(self, manifest: ResourceManifest) -> CatalogState:
        if not isinstance(manifest, ResourceManifest):
            raise CatalogError("register requires a validated ResourceManifest")
        with self.locked():
            current = self.load()
            existing = {item.installation_id: item for item in current.resources}
            if manifest.installation_id in existing:
                if existing[manifest.installation_id] == manifest:
                    return current
                raise CatalogError("installation identity already has different manifest data")
            resources = tuple(sorted((*current.resources, manifest),
                                     key=lambda item: item.installation_id))
            return self._commit(current, CatalogState(
                generation=current.generation + 1,
                resources=resources,
                active=dict(current.active),
                references=dict(current.references),
                activation_history=current.activation_history,
            ))

    def register_many(self, manifests) -> CatalogState:
        """Register a fully validated release set in one catalog generation.

        Catalog-owned activation fields are ignored when checking an already-registered
        installation, which makes release bootstrap repeatable after activation while all
        scientific identity and rights metadata remain immutable.
        """
        values = tuple(manifests)
        if not values or any(not isinstance(item, ResourceManifest) for item in values):
            raise CatalogError("register_many requires one or more validated manifests")
        ids = [item.installation_id for item in values]
        if len(ids) != len(set(ids)):
            raise CatalogError("release registration contains duplicate installation identities")

        def without_catalog_state(item: ResourceManifest) -> ResourceManifest:
            return replace(
                item,
                active_version="",
                superseded_by="",
                rollback_state=RollbackState.CANDIDATE,
            )

        with self.locked():
            current = self.load()
            existing = {item.installation_id: item for item in current.resources}
            additions: list[ResourceManifest] = []
            for manifest in values:
                prior = existing.get(manifest.installation_id)
                if prior is None:
                    additions.append(manifest)
                elif without_catalog_state(prior) != without_catalog_state(manifest):
                    raise CatalogError(
                        "installation identity already has different immutable manifest data")
            if not additions:
                return current
            resources = tuple(sorted(
                (*current.resources, *additions), key=lambda item: item.installation_id))
            return self._commit(current, CatalogState(
                generation=current.generation + 1,
                resources=resources,
                active=dict(current.active),
                references=dict(current.references),
                activation_history=current.activation_history,
            ))

    def replace_manifest(self, manifest: ResourceManifest) -> CatalogState:
        """Update test/compatibility/state evidence without changing installation identity."""
        with self.locked():
            current = self.load()
            existing = self.get_installation(manifest.installation_id, state=current)
            immutable = (
                existing.resource_id, existing.resource_kind, existing.installed_version,
                existing.primary_sha256, existing.install_path,
            )
            replacement_identity = (
                manifest.resource_id, manifest.resource_kind, manifest.installed_version,
                manifest.primary_sha256, manifest.install_path,
            )
            if immutable != replacement_identity:
                raise CatalogError("replacement attempts to change immutable installation identity")
            resources = tuple(manifest if item.installation_id == manifest.installation_id else item
                              for item in current.resources)
            return self._commit(current, CatalogState(
                generation=current.generation + 1,
                resources=resources,
                active=dict(current.active),
                references=dict(current.references),
                activation_history=current.activation_history,
            ))

    def list_versions(self, resource_id: str) -> tuple[ResourceManifest, ...]:
        return tuple(sorted(
            (item for item in self.load().resources if item.resource_id == resource_id),
            key=lambda item: (item.installed_version, item.installation_id),
        ))

    def get_installation(self, installation_id: str, *,
                         state: CatalogState | None = None) -> ResourceManifest:
        state = state or self.load()
        for item in state.resources:
            if item.installation_id == installation_id:
                return item
        raise CatalogError(f"unknown installation: {installation_id}")

    def get_active(self, resource_id: str) -> ResourceManifest | None:
        state = self.load()
        installation_id = state.active.get(resource_id)
        return self.get_installation(installation_id, state=state) if installation_id else None

    def _activate(self, current: CatalogState, resource_id: str, installation_id: str, *,
                  proposal_id: str, action: str) -> CatalogState:
        selected = self.get_installation(installation_id, state=current)
        if selected.resource_id != resource_id:
            raise CatalogError("installation does not belong to the requested resource")
        previous_id = current.active.get(resource_id, "")
        if previous_id == installation_id:
            return current
        resources: list[ResourceManifest] = []
        for item in current.resources:
            if item.resource_id != resource_id:
                resources.append(item)
            elif item.installation_id == installation_id:
                resources.append(replace(
                    item, active_version=selected.installed_version,
                    rollback_state=RollbackState.ACTIVE, superseded_by=""))
            elif item.installation_id == previous_id:
                resources.append(replace(
                    item, active_version=selected.installed_version,
                    rollback_state=(RollbackState.ROLLED_BACK if action == "rollback"
                                    else RollbackState.ROLLBACK_AVAILABLE),
                    superseded_by=installation_id))
            else:
                resources.append(replace(item, active_version=selected.installed_version))
        generation = current.generation + 1
        history = (*current.activation_history, ActivationRecord(
            action=action,
            resource_id=resource_id,
            installation_id=installation_id,
            previous_installation_id=previous_id,
            proposal_id=proposal_id,
            catalog_generation=generation,
        ))
        active = dict(current.active)
        active[resource_id] = installation_id
        return CatalogState(
            generation=generation,
            resources=tuple(sorted(resources, key=lambda item: item.installation_id)),
            active=active,
            references=dict(current.references),
            activation_history=history,
        )

    def activate(self, resource_id: str, installation_id: str, *, proposal_id: str = "") \
            -> CatalogState:
        with self.locked():
            current = self.load()
            next_state = self._activate(
                current, resource_id, installation_id, proposal_id=proposal_id, action="promote")
            return current if next_state is current else self._commit(current, next_state)

    def bootstrap_activate(self, resource_id: str, installation_id: str) -> CatalogState:
        """Activate a verified installed release without representing it as a promotion."""
        with self.locked():
            current = self.load()
            active_installation_id = current.active.get(resource_id, "")
            if active_installation_id and active_installation_id != installation_id:
                raise CatalogError(
                    "bootstrap activation cannot replace an active installation; "
                    "use a reviewed update proposal and explicit promotion"
                )
            next_state = self._activate(
                current, resource_id, installation_id, proposal_id="", action="bootstrap")
            return current if next_state is current else self._commit(current, next_state)

    def rollback_to(self, resource_id: str, installation_id: str, *, proposal_id: str = "") \
            -> CatalogState:
        with self.locked():
            current = self.load()
            next_state = self._activate(
                current, resource_id, installation_id, proposal_id=proposal_id, action="rollback")
            return current if next_state is current else self._commit(current, next_state)

    def record_reference(self, installation_id: str, run_id: str) -> CatalogState:
        if not isinstance(run_id, str) or not run_id.strip():
            raise CatalogError("run_id is required for a resource reference")
        with self.locked():
            current = self.load()
            self.get_installation(installation_id, state=current)
            references = {key: tuple(value) for key, value in current.references.items()}
            run_ids = set(references.get(installation_id, ()))
            if run_id in run_ids:
                return current
            run_ids.add(run_id)
            references[installation_id] = tuple(sorted(run_ids))
            return self._commit(current, CatalogState(
                generation=current.generation + 1,
                resources=current.resources,
                active=dict(current.active),
                references=references,
                activation_history=current.activation_history,
            ))

    def remove_installation(self, installation_id: str) -> CatalogState:
        """Remove only an unreferenced inactive catalog record; installed bytes remain intact."""
        with self.locked():
            current = self.load()
            self.get_installation(installation_id, state=current)
            if installation_id in current.active.values():
                raise ActiveResourceError("active scientific resources cannot be removed")
            if current.references.get(installation_id):
                raise ReferencedResourceError(
                    "resource is retained because one or more RunRecords reference it")
            resources = tuple(item for item in current.resources
                              if item.installation_id != installation_id)
            references = {key: value for key, value in current.references.items()
                          if key != installation_id}
            return self._commit(current, CatalogState(
                generation=current.generation + 1,
                resources=resources,
                active=dict(current.active),
                references=references,
                activation_history=current.activation_history,
            ))
