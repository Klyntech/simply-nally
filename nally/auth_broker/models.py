"""AuthBroker models — LoginSession and related structures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class LoginSession:
    id: str
    user_id: str
    provider: str
    state: str  # raw state (only returned at creation, not persisted)
    state_hash: bytes  # hash for lookup
    authorization_url: str
    redirect_uri: str
    requested_scopes: tuple[str, ...]
    return_surface: str  # cli | telegram | web
    return_reference: str | None
    status: str  # pending | succeeded | denied | failed | expired | consumed
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class AuthorizationStart:
    authorization_url: str
    state: str
    code_verifier: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class CallbackResult:
    success: bool
    provider: str
    user_id: str
    subject: str
    display_name: str | None
    error: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class ProviderIdentity:
    subject: str  # provider user id / sub
    display_name: str | None  # email, username, workspace
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderStatus:
    connected: bool
    subject: str | None
    display_name: str | None
    scopes: tuple[str, ...]
    expires_at: datetime | None
    reauth_required: bool = False
