"""Core OAuth models — typed data structures for the OAuth lifecycle.

These models replace dict-based flow data with explicit, validated structures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Re-export canonical ToolStatus from tools.base (single source of truth)
from nally.tools.base import ToolStatus


@dataclass(frozen=True)
class OAuthToken:
    """Durable credential for a user/provider pair.

    This is what TokenStore persists. Immutable to prevent accidental mutation.
    """

    provider: str
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    account: str | None = None  # display name (email, username)

    @property
    def is_expired(self) -> bool:
        """Check if token is expired."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON storage."""
        return {
            "provider": self.provider,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": list(self.scopes),
            "account": self.account,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthToken:
        """Deserialize from JSON storage."""
        expires_at = data.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        elif isinstance(expires_at, (int, float)):
            expires_at = datetime.fromtimestamp(expires_at, tz=UTC)
        else:
            expires_at = None

        return cls(
            provider=data.get("provider", ""),
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=tuple(data.get("scopes", [])),
            account=data.get("account"),
        )


@dataclass(frozen=True)
class OAuthSession:
    """Temporary state returned by OAuthManager.begin().

    Contains everything the Telegram UI needs to present the OAuth flow
    to the user. The authorization_url is what gets sent as a Telegram button.
    """

    state: str
    provider: str
    authorization_url: str
    code_verifier: str | None = None  # for PKCE flows
    expires_at: datetime | None = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at


@dataclass(frozen=True)
class OAuthResult:
    """Result of a successful OAuth callback.

    Returned by OAuthManager.callback() after token exchange.
    """

    token: OAuthToken
    user_id: str
    provider: str

    @property
    def success(self) -> bool:
        return bool(self.token.access_token)


@dataclass
class MCPToolResult:
    """Structured result from MCP tool execution.

    Replaces string-only results with typed status and optional structured data.
    """

    content: str
    status: ToolStatus = ToolStatus.OK
    structured_content: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    @property
    def success(self) -> bool:
        return self.status == ToolStatus.OK
