"""OAuth flow state — temporary authorization state management.

OAuthFlowStore tracks in-progress OAuth flows. Each flow represents
a single authorization attempt by a single user.

Key properties:
- Flows are short-lived (typically 10 minutes)
- Flows are single-use (consume() atomically retrieves and deletes)
- Flows are keyed by (user_id, state) for multi-user safety
- No global state — each flow is an independent instance

Security invariant:
    state → exactly one flow
    consume() is atomic (prevents replay attacks)
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class OAuthFlow:
    """Temporary state for an in-progress OAuth flow.

    Created by OAuthManager.begin(), consumed by OAuthManager.callback().
    Contains everything needed to complete the OAuth exchange.
    """

    user_id: str
    state: str
    provider: str
    code_verifier: str | None = None  # for PKCE
    code_challenge: str | None = None  # for PKCE
    redirect_uri: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    status: str = "pending"  # pending | completed | failed | expired
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Set default expiry if not provided."""
        if self.expires_at is None:
            self.expires_at = datetime.now(UTC) + timedelta(minutes=10)

    @property
    def is_expired(self) -> bool:
        """Check if flow has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    @property
    def is_usable(self) -> bool:
        """Check if flow can be consumed (pending and not expired)."""
        return self.status == "pending" and not self.is_expired


def generate_state() -> str:
    """Generate a cryptographically secure random state."""
    return secrets.token_urlsafe(32)


def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge."""
    import base64
    import hashlib

    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class OAuthFlowStore:
    """In-memory store for OAuth flows.

    For SaaS deployment, replace with Redis-backed implementation.
    For local development, in-memory is sufficient.

    Keyed by (user_id, state) to support concurrent flows from
    different users without interference.
    """

    def __init__(self, flow_ttl_minutes: int = 10) -> None:
        self._flows: dict[tuple[str, str], OAuthFlow] = {}
        self._flow_ttl_minutes = flow_ttl_minutes

    def create(
        self,
        user_id: str,
        provider: str,
        redirect_uri: str | None = None,
        use_pkce: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> OAuthFlow:
        """Create a new OAuth flow.

        Args:
            user_id: The user initiating the flow
            provider: The OAuth provider (github, google, notion)
            redirect_uri: Where to redirect after authorization
            use_pkce: Whether to generate PKCE verifier/challenge
            metadata: Additional flow metadata

        Returns:
            OAuthFlow with generated state and optional PKCE parameters
        """
        from datetime import timedelta

        state = generate_state()
        code_verifier = None
        code_challenge = None

        if use_pkce:
            code_verifier, code_challenge = generate_pkce_pair()

        flow = OAuthFlow(
            user_id=user_id,
            state=state,
            provider=provider,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(UTC) + timedelta(minutes=self._flow_ttl_minutes),
            metadata=metadata or {},
        )

        key = (user_id, state)
        self._flows[key] = flow
        return flow

    def get(self, user_id: str, state: str) -> OAuthFlow | None:
        """Retrieve a flow without consuming it.

        Returns None if not found or expired.
        """
        key = (user_id, state)
        flow = self._flows.get(key)
        if flow is None:
            return None
        if flow.is_expired:
            self._cleanup(key)
            return None
        return flow

    def consume(self, user_id: str, state: str) -> OAuthFlow | None:
        """Atomically retrieve and delete a flow.

        This is the primary method for completing an OAuth flow.
        Returns None if flow not found, expired, or already consumed.

        The atomic nature prevents replay attacks — a consumed flow
        cannot be used again.
        """
        key = (user_id, state)
        flow = self._flows.pop(key, None)
        if flow is None:
            return None
        if not flow.is_usable:
            return None
        return flow

    def revoke(self, user_id: str, state: str) -> bool:
        """Revoke a flow without completing it. Returns True if revoked."""
        key = (user_id, state)
        flow = self._flows.pop(key, None)
        return flow is not None

    def revoke_all(self, user_id: str) -> int:
        """Revoke all flows for a user. Returns count revoked."""
        count = 0
        keys_to_remove = [k for k in self._flows if k[0] == user_id]
        for key in keys_to_remove:
            del self._flows[key]
            count += 1
        return count

    def cleanup_expired(self) -> int:
        """Remove all expired flows. Returns count removed."""
        now = datetime.now(UTC)
        keys_to_remove = [k for k, f in self._flows.items() if f.expires_at and now >= f.expires_at]
        for key in keys_to_remove:
            del self._flows[key]
        return len(keys_to_remove)

    def _cleanup(self, key: tuple[str, str]) -> None:
        """Internal cleanup of expired flow."""
        self._flows.pop(key, None)

    def __len__(self) -> int:
        return len(self._flows)

    def active_flows_for_user(self, user_id: str) -> list[OAuthFlow]:
        """Return all active (non-expired) flows for a user."""
        return [f for (uid, _), f in self._flows.items() if uid == user_id and f.is_usable]
