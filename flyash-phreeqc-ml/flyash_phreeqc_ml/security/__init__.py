"""Security primitives shared by the local and hosted Virtual LAB editions."""

from .identity import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_VIEWER,
    AuthenticationRequiredError,
    AuthorizationError,
    DeploymentMode,
    IdentityContext,
    IdentityError,
    bind_session_state,
    current_identity,
    identity_context,
    local_single_user_identity,
    resolve_streamlit_identity,
    session_namespace,
    set_current_identity,
)

__all__ = [
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "ROLE_VIEWER",
    "AuthenticationRequiredError",
    "AuthorizationError",
    "DeploymentMode",
    "IdentityContext",
    "IdentityError",
    "bind_session_state",
    "current_identity",
    "identity_context",
    "local_single_user_identity",
    "resolve_streamlit_identity",
    "session_namespace",
    "set_current_identity",
]
