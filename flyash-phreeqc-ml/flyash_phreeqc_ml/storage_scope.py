"""Deterministic tenant/user storage scoping.

Raw OIDC claim values are never used as path components.  Hosted roots and per-user
context filenames are stable SHA-256-derived identifiers, while the explicit local
single-user mode preserves the historical workspace layout for compatibility.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .security.identity import DeploymentMode, IdentityContext, current_identity


class StorageScopeError(RuntimeError):
    """A storage root or scoped path is unsafe."""


def _digest(label: str, value: str) -> str:
    payload = f"wpi-virtual-lab:{label}:v1:{value}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def tenant_directory_name(tenant_id: str) -> str:
    return f"tenant_{_digest('tenant', tenant_id)}"


def subject_file_name(subject_id: str) -> str:
    return f"subject_{_digest('subject', subject_id)}.json"


@dataclass(frozen=True)
class StorageScope:
    """An immutable base/root/context mapping for one authenticated identity."""

    base_root: Path
    identity: IdentityContext
    tenant_root: Path
    active_context_path: Path

    @classmethod
    def for_identity(
        cls,
        base_root: Path | str,
        identity: IdentityContext | None = None,
    ) -> "StorageScope":
        principal = identity or current_identity()
        raw = Path(base_root).expanduser()
        if any(part == ".." for part in raw.parts):
            raise StorageScopeError("storage root must not contain '..'")
        base = raw.absolute()
        if base.exists() and base.is_symlink():
            raise StorageScopeError("storage root must not be a symlink")

        if principal.deployment_mode is DeploymentMode.LOCAL:
            tenant_root = base
            context_path = tenant_root / "active_context.json"
        else:
            tenant_root = base / "tenants" / tenant_directory_name(principal.tenant_id)
            context_path = tenant_root / "contexts" / subject_file_name(principal.subject_id)

        base_resolved = base.resolve(strict=False)
        for candidate in (tenant_root, context_path):
            try:
                candidate.resolve(strict=False).relative_to(base_resolved)
            except ValueError as exc:
                raise StorageScopeError("scoped storage path escapes the base root") from exc
        return cls(base_root=base, identity=principal, tenant_root=tenant_root,
                   active_context_path=context_path)

    @property
    def is_local_compatibility_mode(self) -> bool:
        return self.identity.deployment_mode is DeploymentMode.LOCAL

    @property
    def session_namespace(self) -> str:
        """Opaque namespace suitable for tenant/user-specific in-memory keys."""
        return _digest("session", f"{self.identity.tenant_id}\0{self.identity.subject_id}")

    def to_safe_dict(self, *, include_paths: bool = False) -> dict:
        result = {
            "deployment_mode": self.identity.deployment_mode.value,
            "tenant_scoped": not self.is_local_compatibility_mode,
            "per_user_active_context": not self.is_local_compatibility_mode,
        }
        if include_paths:
            result["base_root"] = str(self.base_root)
            result["tenant_root"] = str(self.tenant_root)
        return result
