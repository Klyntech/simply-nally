"""GitHub OAuth provider — browser-based Authorization Code flow.

This replaces the device flow with standard browser OAuth:
1. User clicks "Connect GitHub"
2. Browser opens to GitHub authorization
3. User authorizes NALLY
4. GitHub redirects to NALLY callback
5. NALLY exchanges code for token
6. Token stored in TokenStore

Environment variables required:
    GITHUB_CLIENT_ID
    GITHUB_CLIENT_SECRET (for web app flow)

Scopes: read:user, repo (configurable via GITHUB_OAUTH_SCOPES)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC

import requests

from nally.oauth.models import OAuthResult, OAuthSession, OAuthToken

logger = logging.getLogger(__name__)

# GitHub OAuth endpoints
_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_API_URL = "https://api.github.com/user"

# Default scopes for v1.
# read:user — read user profile (read-only).
# repo — full repository access (read + write for private repos).
# The "read-only" policy is enforced by convention, not by OAuth scope.
# Override via GITHUB_OAUTH_SCOPES env for least-privilege deployments.
_DEFAULT_SCOPES = "read:user,repo"


class GitHubOAuthError(Exception):
    """Raised when GitHub OAuth operations fail."""


class GitHubProvider:
    """GitHub OAuth via browser-based Authorization Code flow.

    This provider implements the OAuthProvider protocol for GitHub.
    It uses standard browser OAuth, not device flow.
    """

    @property
    def provider_name(self) -> str:
        return "github"

    @property
    def requires_pkce(self) -> bool:
        return False  # GitHub doesn't require PKCE for web apps

    @property
    def scopes(self) -> list[str]:
        scopes_str = os.getenv("GITHUB_OAUTH_SCOPES", _DEFAULT_SCOPES)
        return [s.strip() for s in scopes_str.split(",") if s.strip()]

    async def begin(
        self,
        user_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str | None = None,
    ) -> OAuthSession:
        """Start GitHub OAuth flow.

        Returns authorization_url for the user to visit in their browser.
        """
        client_id = os.getenv("GITHUB_CLIENT_ID", "").strip()
        if not client_id:
            raise GitHubOAuthError(
                "GITHUB_CLIENT_ID not configured. "
                "Register an OAuth App at https://github.com/settings/developers"
            )

        # Build authorization URL with state for CSRF protection
        scopes = " ".join(self.scopes)
        import urllib.parse

        params = {
            "client_id": client_id,
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        auth_url = f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

        return OAuthSession(
            state=state,
            provider="github",
            authorization_url=auth_url,
        )

    async def callback(
        self,
        code: str,
        state: str,
        code_verifier: str | None = None,
        redirect_uri: str | None = None,
    ) -> OAuthResult:
        """Exchange authorization code for token."""
        client_id = os.getenv("GITHUB_CLIENT_ID", "").strip()
        client_secret = os.getenv("GITHUB_CLIENT_SECRET", "").strip()

        if not client_id or not client_secret:
            raise GitHubOAuthError("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET not set.")

        payload: dict[str, str] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        }
        # GitHub requires redirect_uri to match if it was used in authorization
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri

        # Exchange code for token
        try:
            resp = await asyncio.to_thread(
                requests.post,
                _TOKEN_URL,
                json=payload,
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise GitHubOAuthError(f"Token exchange request failed: {exc}") from exc

        if "error" in data:
            raise GitHubOAuthError(f"GitHub OAuth error: {data['error']}")

        access_token = data.get("access_token")
        if not access_token:
            raise GitHubOAuthError(f"No access_token in response: {data}")

        # Fetch user info
        account = None
        try:
            user_resp = await asyncio.to_thread(
                requests.get,
                _USER_API_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if user_resp.ok:
                user_info = user_resp.json()
                account = user_info.get("login", "")
        except Exception:
            pass

        # Build token
        from datetime import datetime, timedelta

        expires_in = data.get("expires_in", 3600)
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        token = OAuthToken(
            provider="github",
            access_token=access_token,
            token_type=data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=tuple(self.scopes),
            account=account,
        )

        # For device flow, we need user_id, but for callback we don't have it
        # This will be handled by OAuthManager which passes user_id
        # For now, return a result without user_id (OAuthManager will set it)
        return OAuthResult(
            token=token,
            user_id="",  # Will be set by OAuthManager
            provider="github",
        )

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        """Refresh an expired GitHub token.

        GitHub tokens don't expire for OAuth Apps, but we implement
        this for completeness. For GitHub, this raises an error
        since refresh tokens aren't available.
        """
        raise GitHubOAuthError(
            "GitHub OAuth tokens don't support refresh. User must re-authenticate."
        )

    async def revoke(self, token: OAuthToken) -> bool:
        """Revoke a GitHub token.

        GitHub doesn't have a standard revocation endpoint for OAuth tokens.
        The user can revoke the app from their GitHub settings.
        """
        # GitHub doesn't support programmatic token revocation
        # for OAuth App tokens. User must revoke manually.
        return True

    async def identity(self, token: OAuthToken):
        """Fetch provider identity for credential subject binding."""
        from nally.auth_broker.models import ProviderIdentity

        try:
            resp = await asyncio.to_thread(
                requests.get,
                _USER_API_URL,
                headers={
                    "Authorization": f"Bearer {token.access_token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if resp.ok:
                info = resp.json()
                subject = str(info.get("id") or info.get("login") or token.account or "github-user")
                display = info.get("login") or info.get("name") or token.account
                return ProviderIdentity(subject=subject, display_name=display, raw=info)
        except Exception:
            pass
        # Fallback to token account
        subj = token.account or "github-user"
        return ProviderIdentity(subject=subj, display_name=token.account, raw={})

    def format_auth_headers(self, token: OAuthToken) -> dict[str, str]:
        """Format token as Authorization header."""
        return {"Authorization": f"Bearer {token.access_token}"}

    def format_auth_env(self, token: OAuthToken) -> dict[str, str]:
        """Format token as environment variable."""
        return {"GITHUB_PERSONAL_ACCESS_TOKEN": token.access_token}
