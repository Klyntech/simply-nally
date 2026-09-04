"""Google OAuth provider — browser-based Authorization Code flow for Gmail.

This replaces the Google device flow with standard browser OAuth:
1. User clicks "Connect Gmail"
2. Browser opens to Google authorization
3. User authorizes NALLY
4. Google redirects to NALLY callback (central HTTPS endpoint)
5. NALLY exchanges code for token
6. Token stored in TokenStore

Environment variables required:
    GMAIL_CLIENT_ID or GOOGLE_CLIENT_ID
    GMAIL_CLIENT_SECRET or GOOGLE_CLIENT_SECRET

Scopes: gmail.readonly + gmail.compose (configurable via GMAIL_OAUTH_SCOPES)
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from datetime import UTC, datetime, timedelta

import requests

from nally.oauth.models import OAuthResult, OAuthSession, OAuthToken

logger = logging.getLogger(__name__)

# Google OAuth endpoints
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Default scopes for Gmail
_DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose"
)


class GoogleOAuthError(Exception):
    """Raised when Google OAuth operations fail."""


class GoogleProvider:
    """Google OAuth via browser-based Authorization Code flow.

    Provider name is "gmail" for backward compatibility with
    TokenStore keys and SUPPORTED_PROVIDERS.
    The underlying OAuth is Google's generic OAuth, scoped for Gmail.
    """

    @property
    def provider_name(self) -> str:
        return "gmail"

    @property
    def requires_pkce(self) -> bool:
        return False

    @property
    def scopes(self) -> list[str]:
        scopes_str = os.getenv("GMAIL_OAUTH_SCOPES", _DEFAULT_SCOPES)
        # Support both space and comma separated
        if "," in scopes_str:
            return [s.strip() for s in scopes_str.split(",") if s.strip()]
        return [s.strip() for s in scopes_str.split() if s.strip()]

    def _client_credentials(self) -> tuple[str, str]:
        """Resolve client_id/secret from env (supports both GMAIL_ and GOOGLE_)."""
        cid = os.getenv("GMAIL_CLIENT_ID", "").strip() or os.getenv("GOOGLE_CLIENT_ID", "").strip()
        csec = (
            os.getenv("GMAIL_CLIENT_SECRET", "").strip()
            or os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        )
        return cid, csec

    async def begin(
        self,
        user_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str | None = None,
    ) -> OAuthSession:
        """Start Google OAuth flow."""
        cid, _ = self._client_credentials()
        if not cid:
            raise GoogleOAuthError(
                "GMAIL_CLIENT_ID (or GOOGLE_CLIENT_ID) not configured. "
                "Create credentials at https://console.cloud.google.com/apis/credentials"
            )

        scopes = " ".join(self.scopes)
        params = {
            "client_id": cid,
            "response_type": "code",
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": state,
            "access_type": "offline",  # request refresh_token
            "prompt": "consent",  # force consent to get refresh_token each time
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        auth_url = f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

        return OAuthSession(
            state=state,
            provider="gmail",
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
        cid, csec = self._client_credentials()
        if not cid or not csec:
            raise GoogleOAuthError("GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET not configured.")

        # Google strictly requires the same redirect_uri as used in begin()
        # This comes from OAuthFlowStore via OAuthManager
        effective_redirect = redirect_uri or os.getenv("OAUTH_BASE_URL", "").strip() or ""

        data: dict[str, str] = {
            "client_id": cid,
            "client_secret": csec,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": effective_redirect,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier

        try:
            resp = await asyncio.to_thread(
                requests.post,
                _TOKEN_URL,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            resp.raise_for_status()
            tdata = resp.json()
        except requests.RequestException as exc:
            raise GoogleOAuthError(f"Token exchange request failed: {exc}") from exc

        if "error" in tdata:
            raise GoogleOAuthError(
                f"Google OAuth error: {tdata.get('error_description', tdata['error'])}"
            )

        access_token = tdata.get("access_token")
        if not access_token:
            raise GoogleOAuthError(f"No access_token in response: {tdata}")

        # Fetch user info for display
        account = None
        try:
            user_resp = await asyncio.to_thread(
                requests.get,
                _USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if user_resp.ok:
                profile = user_resp.json()
                account = profile.get("email", "Gmail user")
            else:
                account = "Gmail user"
        except Exception:
            account = "Gmail user"

        expires_in = tdata.get("expires_in", 3600)
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))

        token = OAuthToken(
            provider="gmail",
            access_token=access_token,
            refresh_token=tdata.get("refresh_token"),
            token_type=tdata.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=tuple(self.scopes),
            account=account,
        )

        return OAuthResult(token=token, user_id="", provider="gmail")

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        """Refresh a Google token using refresh_token."""
        if not token.refresh_token:
            raise GoogleOAuthError("No refresh_token available; re-authentication required.")

        cid, csec = self._client_credentials()
        if not cid or not csec:
            raise GoogleOAuthError("Client credentials not configured for refresh.")

        data = {
            "client_id": cid,
            "client_secret": csec,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }

        try:
            resp = await asyncio.to_thread(
                requests.post,
                _TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            tdata = resp.json()
        except requests.RequestException as exc:
            raise GoogleOAuthError(f"Refresh request failed: {exc}") from exc

        if "error" in tdata:
            raise GoogleOAuthError(f"Google refresh error: {tdata}")

        access_token = tdata.get("access_token")
        if not access_token:
            raise GoogleOAuthError(f"No access_token in refresh response: {tdata}")

        expires_in = tdata.get("expires_in", 3600)
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))

        # Google may return a new refresh_token; keep old if not
        new_refresh = tdata.get("refresh_token", token.refresh_token)

        return OAuthToken(
            provider="gmail",
            access_token=access_token,
            refresh_token=new_refresh,
            token_type=tdata.get("token_type", token.token_type),
            expires_at=expires_at,
            scopes=token.scopes,
            account=token.account,
        )

    async def revoke(self, token: OAuthToken) -> bool:
        """Revoke a Google token."""
        import contextlib

        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                requests.post,
                "https://oauth2.googleapis.com/revoke",
                params={"token": token.access_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        return True

    async def identity(self, token: OAuthToken):
        """Fetch Google identity (userinfo)."""
        from nally.auth_broker.models import ProviderIdentity

        try:
            resp = await asyncio.to_thread(
                requests.get,
                _USERINFO_URL,
                headers={"Authorization": f"Bearer {token.access_token}"},
                timeout=10,
            )
            if resp.ok:
                info = resp.json()
                subject = str(info.get("sub") or info.get("id") or token.account or "google-user")
                display = info.get("email") or token.account
                return ProviderIdentity(subject=subject, display_name=display, raw=info)
        except Exception:
            pass
        subj = token.account or "google-user"
        return ProviderIdentity(subject=subj, display_name=token.account, raw={})

    def format_auth_headers(self, token: OAuthToken) -> dict[str, str]:
        return {"Authorization": f"Bearer {token.access_token}"}

    def format_auth_env(self, token: OAuthToken) -> dict[str, str]:
        return {"GMAIL_TOKEN": token.access_token}


# Alias for backward compatibility — some code imports GmailProvider
GmailProvider = GoogleProvider
