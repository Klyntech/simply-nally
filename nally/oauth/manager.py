"""OAuthManager — canonical facade for OAuth lifecycle.

This is the single entry point for all OAuth operations.
Telegram uses it to initiate flows.
MCP uses it to resolve credentials.
No other module should handle OAuth directly.

Security invariant:
    USER-SCOPED: TokenStore(user_id, provider) → credential exists → use it
                  credential missing → AUTH_REQUIRED (never fallback to global)
    CLI/SYSTEM:  environment credential → use it
"""

from __future__ import annotations

import logging
from typing import Any

from .flow_store import OAuthFlow, OAuthFlowStore
from .models import OAuthResult, OAuthSession, OAuthToken
from .providers.base import OAuthProvider
from .token_store import TokenStore, TokenStoreError

logger = logging.getLogger(__name__)


class OAuthError(Exception):
    """Raised when OAuth operations fail."""


class OAuthManager:
    """Canonical facade for OAuth lifecycle.

    This class coordinates:
    - OAuthFlowStore (temporary authorization state)
    - TokenStore (durable credentials)
    - OAuthProvider implementations (GitHub, Google, Notion)

    Usage from Telegram:
        manager = OAuthManager()
        session = await manager.begin(user_id="123", provider="github")
        # Send session.authorization_url to user
        result = await manager.callback(user_id="123", state=state, code=code)

    Usage from MCP:
        manager = OAuthManager()
        credential = manager.get_credential(user_id="123", provider="github")
        if credential is None:
            return AUTH_REQUIRED
    """

    def __init__(
        self,
        token_store: TokenStore | None = None,
        flow_store: OAuthFlowStore | None = None,
    ) -> None:
        self._token_store = token_store or TokenStore()
        self._flow_store = flow_store or OAuthFlowStore()
        self._providers: dict[str, OAuthProvider] = {}

    def register_provider(self, provider: OAuthProvider) -> None:
        """Register an OAuth provider."""
        self._providers[provider.provider_name] = provider

    def _get_provider(self, provider_name: str) -> OAuthProvider:
        """Get a registered provider."""
        provider = self._providers.get(provider_name)
        if provider is None:
            raise OAuthError(
                f"Unknown provider: {provider_name}. "
                f"Supported: {', '.join(sorted(self._providers.keys()))}"
            )
        return provider

    # ------------------------------------------------------------------
    # Flow initiation
    # ------------------------------------------------------------------

    async def begin(
        self,
        user_id: str,
        provider_name: str,
        redirect_uri: str | None = None,
    ) -> OAuthSession:
        """Start an OAuth flow.

        Returns an OAuthSession with authorization_url that should be
        sent to the Telegram user as a button link.

        The flow is stored in OAuthFlowStore and will be consumed
        when the user completes authorization.
        """
        provider = self._get_provider(provider_name)

        # Create flow in store (generates state + PKCE if needed)
        flow = self._flow_store.create(
            user_id=user_id,
            provider=provider_name,
            redirect_uri=redirect_uri,
            use_pkce=provider.requires_pkce,
        )

        # Get authorization URL from provider (must use flow's state/challenge)
        session = await provider.begin(
            user_id=user_id,
            redirect_uri=redirect_uri or "",
            state=flow.state,
            code_challenge=flow.code_challenge,
        )

        # Return session with flow state
        return OAuthSession(
            state=flow.state,
            provider=provider_name,
            authorization_url=session.authorization_url,
            code_verifier=flow.code_verifier,
            expires_at=flow.expires_at,
        )

    # ------------------------------------------------------------------
    # Callback handling
    # ------------------------------------------------------------------

    async def callback(
        self,
        user_id: str,
        state: str,
        code: str,
        provider_name: str | None = None,
    ) -> OAuthResult:
        """Handle OAuth callback.

        Consumes the flow atomically and exchanges the code for a token.
        Stores the token in TokenStore.

        Args:
            user_id: The user who initiated the flow
            state: OAuth state parameter
            code: Authorization code from provider
            provider_name: Optional provider override (inferred from flow)

        Returns:
            OAuthResult with token and user info

        Raises:
            OAuthError: If flow not found, expired, or code exchange fails
        """
        # Find and consume the flow
        flow = self._flow_store.consume(user_id, state)
        if flow is None:
            raise OAuthError("Invalid or expired OAuth state")

        # Get provider (from flow or explicit)
        provider_name = provider_name or flow.provider
        provider = self._get_provider(provider_name)

        # Exchange code for token (must use same redirect_uri as begin)
        try:
            result = await provider.callback(
                code=code,
                state=state,
                code_verifier=flow.code_verifier,
                redirect_uri=flow.redirect_uri,
            )
        except Exception as exc:
            flow.status = "failed"
            raise OAuthError(f"Token exchange failed: {exc}") from exc

        # Store token
        if result.success:
            try:
                self._token_store.put(user_id, provider_name, result.token)
            except TokenStoreError as exc:
                raise OAuthError(f"Token exchange succeeded but storage failed: {exc}") from exc

        return result

    # ------------------------------------------------------------------
    # Credential resolution (MCP consumes this)
    # ------------------------------------------------------------------

    def get_credential(
        self,
        user_id: str,
        provider_name: str,
    ) -> OAuthToken | None:
        """Get a valid credential for a user/provider.

        Returns None if no credential exists or if expired.
        Does NOT fall back to global/environment credentials.

        This is the method MCP should call to resolve credentials.
        """
        return self._token_store.get_valid(user_id, provider_name)

    def get_credential_or_raise(
        self,
        user_id: str,
        provider_name: str,
    ) -> OAuthToken:
        """Get a valid credential, raising OAuthError if missing.

        Use this when the caller wants to fail explicitly
        rather than handle None.
        """
        token = self.get_credential(user_id, provider_name)
        if token is None:
            raise OAuthError(
                f"No valid credential for {provider_name}. User must authenticate via /mcp connect."
            )
        return token

    def has_credential(self, user_id: str, provider_name: str) -> bool:
        """Check if user has a valid credential."""
        return self._token_store.is_valid(user_id, provider_name)

    # ------------------------------------------------------------------
    # Credential management
    # ------------------------------------------------------------------

    def disconnect(self, user_id: str, provider_name: str) -> bool:
        """Remove a user's credential. Returns True if was connected."""
        return self._token_store.delete(user_id, provider_name)

    def disconnect_all(self, user_id: str) -> int:
        """Remove all credentials for a user. Returns count removed."""
        return self._token_store.delete_all(user_id)

    def status(self, user_id: str) -> dict[str, dict[str, Any]]:
        """Return status for all providers for a user.

        Returns:
            {
                "github": {"connected": True, "account": "username"},
                "gmail": {"connected": False, "account": None},
                "notion": {"connected": True, "account": "workspace"},
            }
        """
        result: dict[str, dict[str, Any]] = {}
        for name in self._providers:
            token = self._token_store.get(user_id, name)
            result[name] = {
                "connected": token is not None and not token.is_expired,
                "account": token.account if token else None,
            }
        return result

    # ------------------------------------------------------------------
    # Auth formatting for MCP transport
    # ------------------------------------------------------------------

    def get_auth_headers(self, user_id: str, provider_name: str) -> dict[str, str] | None:
        """Return auth headers for MCP HTTP transport, or None.

        Returns formatted headers if credential exists, None otherwise.
        Does NOT fall back to environment variables.
        """
        token = self.get_credential(user_id, provider_name)
        if token is None:
            return None
        provider = self._get_provider(provider_name)
        return provider.format_auth_headers(token)

    def get_auth_env(self, user_id: str, provider_name: str) -> dict[str, str] | None:
        """Return auth env vars for MCP stdio transport, or None.

        Returns formatted env vars if credential exists, None otherwise.
        Does NOT fall back to environment variables.
        """
        token = self.get_credential(user_id, provider_name)
        if token is None:
            return None
        provider = self._get_provider(provider_name)
        return provider.format_auth_env(token)

    # ------------------------------------------------------------------
    # Flow management
    # ------------------------------------------------------------------

    def get_active_flows(self, user_id: str) -> list[OAuthFlow]:
        """Return all active (non-expired) flows for a user."""
        return self._flow_store.active_flows_for_user(user_id)

    def cleanup_expired_flows(self) -> int:
        """Remove all expired flows. Returns count removed."""
        return self._flow_store.cleanup_expired()


# ------------------------------------------------------------------
# Singleton — shared across Telegram and callback handler
# ------------------------------------------------------------------
_default_manager: OAuthManager | None = None


def get_oauth_manager() -> OAuthManager:
    """Return shared OAuthManager singleton with providers registered."""
    global _default_manager
    if _default_manager is not None:
        return _default_manager

    import contextlib

    from nally.oauth.providers.github import GitHubProvider
    from nally.oauth.providers.google import GoogleProvider
    from nally.oauth.providers.notion import NotionProvider

    mgr = OAuthManager()
    for p in (GitHubProvider(), GoogleProvider(), NotionProvider()):
        with contextlib.suppress(Exception):
            mgr.register_provider(p)
    _default_manager = mgr
    return mgr


def reset_oauth_manager() -> None:
    """Reset singleton (for tests)."""
    global _default_manager
    _default_manager = None
