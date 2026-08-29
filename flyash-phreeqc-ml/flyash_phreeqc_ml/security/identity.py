"""Immutable identity and authorization context for local and hosted execution.

The local application remains deliberately usable without an identity provider, but
that state is explicit: it receives a synthetic, single-user identity in the
``local`` tenant.  Hosted mode is the opposite.  It never falls back to the local
identity and never trusts browser-supplied query/session values; a verified
``st.user`` OIDC principal is required before any durable store is constructed.

Only stable opaque claim values are retained.  Tokens, cookies, e-mail addresses and
the complete OIDC claim document are neither copied nor exposed by this module.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, MutableMapping


DEPLOYMENT_MODE_ENV = "VLAB_DEPLOYMENT_MODE"
TENANT_CLAIM_ENV = "VLAB_OIDC_TENANT_CLAIM"
ROLE_CLAIM_ENV = "VLAB_OIDC_ROLE_CLAIM"
ALLOWED_SUBJECTS_ENV = "VLAB_ALLOWED_SUBJECTS"
ALLOWED_TENANTS_ENV = "VLAB_ALLOWED_TENANTS"
VIEWER_SUBJECTS_ENV = "VLAB_VIEWER_SUBJECTS"
VIEWER_TENANTS_ENV = "VLAB_VIEWER_TENANTS"
MEMBER_SUBJECTS_ENV = "VLAB_MEMBER_SUBJECTS"
MEMBER_TENANTS_ENV = "VLAB_MEMBER_TENANTS"
ADMIN_SUBJECTS_ENV = "VLAB_ADMIN_SUBJECTS"

ROLE_VIEWER = "viewer"
ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"
VALID_ROLES = frozenset({ROLE_VIEWER, ROLE_MEMBER, ROLE_ADMIN})

_CURRENT_IDENTITY: ContextVar[IdentityContext | None] = ContextVar(
    "virtual_lab_current_identity", default=None)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SESSION_NAMESPACE_KEY = "_vl_identity_namespace"


class IdentityError(RuntimeError):
    """Base class for controlled identity failures."""


class AuthenticationRequiredError(IdentityError):
    """Hosted execution has no verified authenticated principal."""


class AuthorizationError(IdentityError):
    """The authenticated principal is not allowed to perform an operation."""


class DeploymentMode(str, Enum):
    LOCAL = "local"
    HOSTED = "hosted"


def deployment_mode(environ: Mapping[str, str] | None = None) -> DeploymentMode:
    """Resolve the deployment mode, rejecting ambiguous or misspelled values.

    The historical native application defaults to local single-user mode.  Hosted
    operators must opt in with ``VLAB_DEPLOYMENT_MODE=hosted``; once selected, all
    missing/invalid identity state fails closed.
    """
    source = os.environ if environ is None else environ
    raw = str(source.get(DEPLOYMENT_MODE_ENV, DeploymentMode.LOCAL.value)).strip().lower()
    try:
        return DeploymentMode(raw)
    except ValueError as exc:
        raise IdentityError(
            f"{DEPLOYMENT_MODE_ENV} must be 'local' or 'hosted'") from exc


def _opaque_claim(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or _CONTROL_RE.search(text):
        raise AuthenticationRequiredError(f"authenticated identity has no usable {label}")
    return text


def _csv_set(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        items = (value,)
    return frozenset(str(item).strip() for item in items if str(item).strip())


@dataclass(frozen=True)
class IdentityContext:
    """Small immutable authorization context; contains no credential material."""

    subject_id: str
    tenant_id: str
    roles: frozenset[str]
    deployment_mode: DeploymentMode
    authenticated: bool = True

    def __post_init__(self) -> None:
        subject = _opaque_claim(self.subject_id, "subject")
        tenant = _opaque_claim(self.tenant_id, "tenant")
        roles = frozenset(str(role).strip().lower() for role in self.roles)
        unknown = roles - VALID_ROLES
        if unknown:
            raise AuthorizationError(f"unsupported identity roles: {sorted(unknown)}")
        if not roles:
            raise AuthorizationError("identity must have at least one role")
        mode = DeploymentMode(self.deployment_mode)
        if mode is DeploymentMode.HOSTED and not self.authenticated:
            raise AuthenticationRequiredError("hosted identity is not authenticated")
        object.__setattr__(self, "subject_id", subject)
        object.__setattr__(self, "tenant_id", tenant)
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "deployment_mode", mode)

    @property
    def is_hosted(self) -> bool:
        return self.deployment_mode is DeploymentMode.HOSTED

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    def has_role(self, role: str) -> bool:
        wanted = str(role).strip().lower()
        if ROLE_ADMIN in self.roles:
            return wanted in VALID_ROLES
        if wanted == ROLE_VIEWER and ROLE_MEMBER in self.roles:
            return True
        return wanted in self.roles

    def require_role(self, role: str) -> None:
        if not self.has_role(role):
            raise AuthorizationError(f"operation requires the {role!r} role")

    def to_safe_dict(self) -> dict:
        """Return non-secret diagnostics without leaking the raw subject/tenant claims."""
        return {
            "deployment_mode": self.deployment_mode.value,
            "authenticated": self.authenticated,
            "roles": sorted(self.roles),
            "administrator": self.is_admin,
        }


def local_single_user_identity() -> IdentityContext:
    """Return the explicit identity used by the backwards-compatible local edition."""
    return IdentityContext(
        subject_id="local-single-user",
        tenant_id="local",
        roles=frozenset({ROLE_ADMIN}),
        deployment_mode=DeploymentMode.LOCAL,
        authenticated=True,
    )


def session_namespace(identity: IdentityContext) -> str:
    """Opaque per-principal namespace for in-memory UI state; contains no raw claims."""
    payload = (
        f"wpi-virtual-lab:session:v1:{identity.tenant_id}\0{identity.subject_id}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def bind_session_state(
    state: MutableMapping[str, object], identity: IdentityContext,
) -> bool:
    """Bind hosted state to one principal, clearing stale AI/scientific caches.

    State without an identity marker is untrusted and is cleared on the first hosted
    binding. Local single-user compatibility mode is intentionally unchanged.
    """
    if not identity.is_hosted:
        return False
    namespace = session_namespace(identity)
    if state.get(SESSION_NAMESPACE_KEY) == namespace:
        return False
    state.clear()
    state[SESSION_NAMESPACE_KEY] = namespace
    return True


def set_current_identity(identity: IdentityContext) -> Token:
    """Bind an immutable identity to the current request/task context."""
    if not isinstance(identity, IdentityContext):
        raise IdentityError("current identity must be an IdentityContext")
    return _CURRENT_IDENTITY.set(identity)


def reset_current_identity(token: Token) -> None:
    _CURRENT_IDENTITY.reset(token)


def current_identity() -> IdentityContext:
    """Return the bound identity; local-only callers receive the explicit local identity.

    Hosted mode never falls back: storage/audit code invoked without an app-bound
    authenticated identity raises before resolving a global path.
    """
    identity = _CURRENT_IDENTITY.get()
    if identity is not None:
        return identity
    if deployment_mode() is DeploymentMode.HOSTED:
        raise AuthenticationRequiredError(
            "hosted operation requires an authenticated request identity")
    return local_single_user_identity()


@contextmanager
def identity_context(identity: IdentityContext):
    """Temporarily bind identity for scripts/tests/background request execution."""
    token = set_current_identity(identity)
    try:
        yield identity
    finally:
        reset_current_identity(token)


def _claim(user: object, name: str) -> object | None:
    if isinstance(user, Mapping):
        return user.get(name)
    try:
        return getattr(user, name)
    except Exception:
        return None


def _logged_in(user: object) -> bool:
    value = _claim(user, "is_logged_in")
    return value is True


def resolve_streamlit_identity(
    streamlit_module,
    *,
    environ: Mapping[str, str] | None = None,
) -> IdentityContext:
    """Resolve a trusted app identity before constructing any tenant-aware service.

    In hosted mode this accepts only claims exposed by Streamlit's verified ``st.user``
    surface.  Missing authentication, missing subject/tenant claims, or an optional
    deployment allow-list mismatch all fail closed.
    """
    source = os.environ if environ is None else environ
    mode = deployment_mode(source)
    if mode is DeploymentMode.LOCAL:
        return local_single_user_identity()

    try:
        user = streamlit_module.user
    except Exception as exc:
        raise AuthenticationRequiredError(
            "hosted mode requires configured Streamlit OIDC authentication") from exc
    if not _logged_in(user):
        raise AuthenticationRequiredError("sign-in is required")

    subject = _opaque_claim(_claim(user, "sub"), "subject")
    tenant_claim = str(source.get(TENANT_CLAIM_ENV, "sub") or "sub").strip()
    tenant = _opaque_claim(_claim(user, tenant_claim), "tenant")

    expires = _claim(user, "exp")
    try:
        expires_at = float(expires)
    except (TypeError, ValueError) as exc:
        raise AuthenticationRequiredError(
            "authenticated identity has no usable expiration claim") from exc
    if expires_at <= time.time():
        raise AuthenticationRequiredError("authenticated identity has expired")

    allowed_subjects = _csv_set(source.get(ALLOWED_SUBJECTS_ENV))
    allowed_tenants = _csv_set(source.get(ALLOWED_TENANTS_ENV))
    if not allowed_subjects and not allowed_tenants:
        raise AuthorizationError(
            "hosted mode requires a separate subject or tenant invite allow-list")
    if allowed_subjects and subject not in allowed_subjects:
        raise AuthorizationError("authenticated subject is not invited")
    if allowed_tenants and tenant not in allowed_tenants:
        raise AuthorizationError("authenticated tenant is not invited")

    # OIDC establishes identity only. Even a signed ``roles`` claim is not this
    # application's authorization policy: invite, viewer/member, and administrator
    # rights are separate deployment-owned allow-lists. Missing role authorization
    # fails closed instead of silently upgrading an invited principal to member.
    if subject in _csv_set(source.get(ADMIN_SUBJECTS_ENV)):
        roles = {ROLE_ADMIN}
    elif subject in _csv_set(source.get(MEMBER_SUBJECTS_ENV)) \
            or tenant in _csv_set(source.get(MEMBER_TENANTS_ENV)):
        roles = {ROLE_MEMBER}
    elif subject in _csv_set(source.get(VIEWER_SUBJECTS_ENV)) \
            or tenant in _csv_set(source.get(VIEWER_TENANTS_ENV)):
        roles = {ROLE_VIEWER}
    else:
        raise AuthorizationError(
            "authenticated identity has no administrator-configured application role")
    return IdentityContext(
        subject_id=subject,
        tenant_id=tenant,
        roles=frozenset(roles),
        deployment_mode=DeploymentMode.HOSTED,
        authenticated=True,
    )
