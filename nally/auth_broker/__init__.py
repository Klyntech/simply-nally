"""AuthBroker — single browser-first authorization system.

Responsibilities: create login session, construct provider authorization URL,
validate and consume callback, hand credential reference to vault.

Must not be an LLM tool, must not expose tokens to Telegram, must not let
agent inspect raw credentials.

Flow:
  CLI:  AuthBroker.start(user_id, provider, cli) -> open browser -> loopback -> handle_callback -> vault
  Telegram: AuthBroker.start(user_id, provider, telegram) -> send link -> central callback -> handle_callback -> vault -> completion event
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import CallbackResult, LoginSession, ProviderIdentity

logger = logging.getLogger(__name__)

# Session expiry default
SESSION_TTL_MINUTES = 10


def _generate_state() -> str:
    return secrets.token_urlsafe(32)


def _hash_state(state: str) -> bytes:
    return hashlib.sha256(state.encode()).digest()


def _generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _build_callback_url(provider: str, redirect_uri: str | None = None) -> str:
    if redirect_uri:
        return redirect_uri
    base = os.getenv("OAUTH_BASE_URL", "").strip() or os.getenv("WEBHOOK_BASE_URL", "").strip()
    if base:
        return f"{base.rstrip('/')}/oauth/callback/{provider}"
    # For local dev without base, default to localhost:8080 (legacy) but ideally caller provides loopback
    port = os.getenv("OAUTH_CALLBACK_PORT", "8080")
    return f"http://localhost:{port}/oauth/callback/{provider}"


class AuthBroker:
    """Single authorization entry point."""

    def __init__(self, vault=None, directory=None):
        from nally.vault import get_vault

        from nally.directory import get_directory

        self._vault = vault or get_vault()
        self._directory = directory or get_directory()
        self._providers: dict[str, Any] = {}
        # In-memory fallback for login_sessions when DB not configured
        self._mem_sessions: dict[bytes, dict[str, Any]] = {}

    def register_provider(self, provider: Any) -> None:
        name = getattr(provider, "provider_name", None) or getattr(provider, "name", None)
        if not name:
            raise ValueError("Provider must have provider_name or name")
        self._providers[name] = provider
        # Also register alias for gmail/google interop
        if name == "gmail":
            self._providers["google"] = provider
        if name == "google":
            self._providers["gmail"] = provider

    def _get_provider(self, provider_name: str):
        p = self._providers.get(provider_name)
        if p is None:
            # Try lazy import of known providers
            if provider_name in ("github", "gmail", "google", "notion"):
                self._load_builtin_providers()
                p = self._providers.get(provider_name)
        if p is None:
            raise ValueError(f"Unknown provider: {provider_name}. Supported: {', '.join(sorted(self._providers.keys()))}")
        return p

    def _load_builtin_providers(self):
        import contextlib

        try:
            from nally.oauth.providers.github import GitHubProvider

            if "github" not in self._providers:
                self.register_provider(GitHubProvider())
        except Exception:
            pass
        try:
            from nally.oauth.providers.google import GoogleProvider

            if "gmail" not in self._providers and "google" not in self._providers:
                self.register_provider(GoogleProvider())
        except Exception:
            pass
        try:
            from nally.oauth.providers.notion import NotionProvider

            if "notion" not in self._providers:
                self.register_provider(NotionProvider())
        except Exception:
            pass

    def _db_configured(self) -> bool:
        try:
            from nally import db

            return db.is_configured()
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start(
        self,
        user_id: str,
        provider: str,
        return_surface: str = "cli",
        return_reference: str | None = None,
        scopes: list[str] | None = None,
        redirect_uri: str | None = None,
    ) -> LoginSession:
        """Create pending session and return authorization URL."""
        provider_obj = self._get_provider(provider)
        state = _generate_state()
        state_hash = _hash_state(state)
        # PKCE
        requires_pkce = getattr(provider_obj, "requires_pkce", False)
        # provider may have property
        if callable(requires_pkce):
            requires_pkce = requires_pkce
        else:
            requires_pkce = bool(requires_pkce)
        code_verifier = None
        code_challenge = None
        if requires_pkce:
            code_verifier, code_challenge = _generate_pkce()
        else:
            # Still generate for providers that optionally support PKCE? Spec says use PKCE where supported.
            # For Google we don't, for GitHub we don't.
            pass

        # Determine redirect_uri
        if not redirect_uri:
            if return_surface == "cli":
                # For CLI, caller may want loopback; but we default to central if OAUTH_BASE_URL set
                # If central base not set, use placeholder that LoopbackServer will override
                redirect_uri = _build_callback_url(provider)
            else:
                redirect_uri = _build_callback_url(provider)

        # Store PKCE verifier encrypted
        from nally.vault.crypto import encrypt, _load_master_key

        key = _load_master_key()
        # Use AAD = user_id:provider:state_hash hex
        aad = f"{user_id}:{provider}:{state_hash.hex()}".encode()
        verifier_blob = encrypt((code_verifier or "").encode(), aad, key) if code_verifier else b""

        expires_at = datetime.now(UTC) + timedelta(minutes=SESSION_TTL_MINUTES)
        requested_scopes = tuple(scopes or getattr(provider_obj, "scopes", []) or [])

        # Persist session
        session_id = str(uuid.uuid4())
        # Only attempt DB if user_id looks like UUID and DB reachable
        use_db = self._db_configured()
        if use_db:
            # Check if user_id is UUID (synthetic like _global falls back to memory)
            try:
                __import__("uuid").UUID(user_id)
            except Exception:
                use_db = False
        if use_db:
            try:
                from nally import db

                conn = db.pooled_connect()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO login_sessions (
                                id, user_id, provider, state_hash, pkce_verifier_encrypted,
                                redirect_uri, requested_scopes, return_surface, return_reference,
                                status, expires_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, 'pending', %s)
                            """,
                            (
                                session_id,
                                user_id,
                                provider,
                                state_hash,
                                verifier_blob if verifier_blob else b"",
                                redirect_uri,
                                __import__("json").dumps(list(requested_scopes)),
                                return_surface,
                                return_reference,
                                expires_at,
                            ),
                        )
                        conn.commit()
                finally:
                    conn.close()
            except Exception as exc:
                logger.warning("DB login_sessions insert failed, falling back to memory: %s", exc)
                use_db = False
        if not use_db:
            # In-memory fallback
            self._mem_sessions[state_hash] = {
                "id": session_id,
                "user_id": user_id,
                "provider": provider,
                "state": state,
                "state_hash": state_hash,
                "pkce_verifier_encrypted": verifier_blob,
                "pkce_verifier": code_verifier or "",
                "redirect_uri": redirect_uri,
                "requested_scopes": requested_scopes,
                "return_surface": return_surface,
                "return_reference": return_reference,
                "status": "pending",
                "expires_at": expires_at,
                "created_at": datetime.now(UTC),
            }

        # Ask provider for authorization URL
        # Provider expects (user_id, redirect_uri, state, code_challenge)
        try:
            oauth_session = await provider_obj.begin(
                user_id=user_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
            )
            auth_url = oauth_session.authorization_url
        except TypeError as exc:
            # Fallback for providers with different signature
            logger.debug("Provider begin signature fallback: %s", exc)
            # Try with 3 args
            try:
                oauth_session = await provider_obj.begin(user_id, redirect_uri, state)  # type: ignore
                auth_url = oauth_session.authorization_url
            except Exception as exc2:
                raise RuntimeError(f"Provider {provider} begin failed: {exc2}") from exc2
        # If provider didn't include PKCE, we already stored verifier; provider will use challenge we passed
        # Need to ensure auth URL contains state and PKCE challenge where needed

        # Create return object (with raw state for not-yet-consumed session)
        # Log with correlation id, never log raw state or tokens
        correlation = session_id[:8]
        logger.info(
            "auth.start provider=%s user=%s surface=%s session=%s expires=%s",
            provider,
            user_id[:8],
            return_surface,
            correlation,
            expires_at.isoformat(),
        )

        return LoginSession(
            id=session_id,
            user_id=user_id,
            provider=provider,
            state=state,
            state_hash=state_hash,
            authorization_url=auth_url,
            redirect_uri=redirect_uri,
            requested_scopes=tuple(requested_scopes),
            return_surface=return_surface,
            return_reference=return_reference,
            status="pending",
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # Handle callback — the critical security boundary
    # ------------------------------------------------------------------

    async def handle_callback(self, provider: str, query: dict[str, Any]) -> CallbackResult:
        """Validate callback, exchange code, store credential, mark success.

        Query should contain code, state, error, error_description.
        This is the ONLY place where code exchange happens.
        """
        # Normalize
        code = query.get("code")
        state = query.get("state")
        error = query.get("error")
        error_desc = query.get("error_description") or query.get("errorDescription")
        if isinstance(code, list):
            code = code[0] if code else None
        if isinstance(state, list):
            state = state[0] if state else None
        if isinstance(error, list):
            error = error[0] if error else None

        correlation = secrets.token_hex(4)

        if error:
            msg = error_desc or error
            logger.warning("callback error provider=%s error=%s corr=%s", provider, error, correlation)
            # Find session by state if available to mark denied
            if state:
                await self._mark_session(state, provider, "denied")
            return CallbackResult(
                success=False,
                provider=provider,
                user_id="",
                subject="",
                display_name=None,
                error=f"Authorization was cancelled: {msg}",
                correlation_id=correlation,
            )

        if not code or not state:
            logger.warning("callback missing code/state provider=%s corr=%s", provider, correlation)
            return CallbackResult(
                success=False,
                provider=provider,
                user_id="",
                subject="",
                display_name=None,
                error="Missing code or state in callback",
                correlation_id=correlation,
            )

        # Atomic lookup and consume
        session = await self._consume_session(state, provider, correlation)
        if session is None:
            # Already consumed or invalid — generic message
            logger.warning("callback state invalid/expired provider=%s corr=%s state=%s", provider, correlation, state[:8])
            return CallbackResult(
                success=False,
                provider=provider,
                user_id="",
                subject="",
                display_name=None,
                error="Link expired or invalid. Please try connecting again.",
                correlation_id=correlation,
            )

        user_id = session["user_id"]
        redirect_uri = session["redirect_uri"]
        # Decrypt PKCE verifier
        from nally.vault.crypto import decrypt, _load_master_key

        key = _load_master_key()
        aad = f"{user_id}:{provider}:{_hash_state(state).hex()}".encode()
        code_verifier = None
        blob = session.get("pkce_verifier_encrypted")
        if blob:
            if isinstance(blob, memoryview):
                blob = blob.tobytes()
            if isinstance(blob, str):
                # legacy file stored as bytes repr? handle
                blob = blob.encode()
            if blob and blob != b"":
                try:
                    # In-memory stores plaintext verifier under "pkce_verifier" key
                    if session.get("pkce_verifier"):
                        code_verifier = session.get("pkce_verifier")
                    else:
                        raw = decrypt(bytes(blob), aad, key)
                        code_verifier = raw.decode() if raw else None
                        if code_verifier == "":
                            code_verifier = None
                except Exception as exc:
                    logger.warning("PKCE decrypt failed corr=%s: %s", correlation, exc)
                    await self._update_session_status(session["id"], "failed")
                    return CallbackResult(
                        success=False,
                        provider=provider,
                        user_id=user_id,
                        subject="",
                        display_name=None,
                        error="Link expired or invalid. Please try again.",
                        correlation_id=correlation,
                    )

        # Exchange via provider
        provider_obj = self._get_provider(provider)
        try:
            result = await provider_obj.callback(
                code=code,
                state=state,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
            )
        except Exception as exc:
            logger.warning("callback exchange failed provider=%s corr=%s: %s", provider, correlation, exc)
            await self._update_session_status(session["id"], "failed")
            return CallbackResult(
                success=False,
                provider=provider,
                user_id=user_id,
                subject="",
                display_name=None,
                error="Provider exchange failed. Please try again.",
                correlation_id=correlation,
            )

        # Validate identity — must have identity lookup
        # result.token may contain account, but we prefer provider.identity()
        subject = None
        display_name = None
        raw_meta = {}
        try:
            # Try provider.identity if available
            identity_fn = getattr(provider_obj, "identity", None)
            if identity_fn:
                ident = await provider_obj.identity(result.token)  # type: ignore
                if ident:
                    subject = getattr(ident, "subject", None) or getattr(ident, "id", None) or result.token.account
                    display_name = getattr(ident, "display_name", None) or getattr(ident, "login", None) or result.token.account
                    raw_meta = getattr(ident, "raw", None) or {}
            if not subject:
                subject = result.token.account or "unknown"
            if not display_name:
                display_name = result.token.account
        except Exception as exc:
            logger.warning("identity lookup failed provider=%s corr=%s: %s", provider, correlation, exc)
            await self._update_session_status(session["id"], "failed")
            return CallbackResult(
                success=False,
                provider=provider,
                user_id=user_id,
                subject="",
                display_name=None,
                error="Could not verify the provider account. Please try again.",
                correlation_id=correlation,
            )

        if not subject or subject == "unknown":
            # Still allow but log
            subject = result.token.account or f"{provider}-user"

        # Store credential in vault (atomic upsert)
        # Include resource audience and scopes
        provider_metadata = {
            "account": display_name,
            "provider_account": result.token.account,
            "scopes": list(result.token.scopes) if result.token.scopes else [],
        }
        if raw_meta:
            provider_metadata["raw"] = raw_meta
        # If provider is remote MCP, store resource
        # For now store provider name as resource audience
        provider_metadata["resource"] = provider

        try:
            vault_cred = self._vault.put(
                user_id=user_id,
                provider=provider,
                subject=subject,
                access_token=result.token.access_token,
                refresh_token=result.token.refresh_token,
                token_type=result.token.token_type or "Bearer",
                scopes=list(result.token.scopes) if result.token.scopes else [],
                expires_at=result.token.expires_at,
                provider_metadata=provider_metadata,
            )
        except Exception as exc:
            logger.warning("vault put failed provider=%s corr=%s: %s", provider, correlation, exc)
            await self._update_session_status(session["id"], "failed")
            return CallbackResult(
                success=False,
                provider=provider,
                user_id=user_id,
                subject=subject,
                display_name=display_name,
                error="Could not store credential. Please try again.",
                correlation_id=correlation,
            )

        # Link provider identity in directory (best-effort)
        try:
            self._directory.link_provider_identity(user_id, provider, subject, display_name)
        except Exception:
            pass

        # Mark session succeeded
        await self._update_session_status(session["id"], "succeeded")

        # Invalidate MCP tool cache for this user/provider
        try:
            from nally.mcp.broker import get_broker

            broker = get_broker()
            await broker.invalidate_cache(user_id, provider)
        except Exception:
            pass

        logger.info(
            "callback success provider=%s user=%s subject=%s corr=%s",
            provider,
            user_id[:8],
            subject[:12] if subject else "unknown",
            correlation,
        )

        return CallbackResult(
            success=True,
            provider=provider,
            user_id=user_id,
            subject=subject,
            display_name=display_name,
            correlation_id=correlation,
        )

    # ------------------------------------------------------------------
    # Session storage helpers (DB vs memory)
    # ------------------------------------------------------------------

    async def _consume_session(self, state: str, provider: str, correlation: str) -> dict[str, Any] | None:
        state_hash = _hash_state(state)
        if self._db_configured():
            try:
                from nally import db

                conn = db.pooled_connect()
                try:
                    with conn.cursor() as cur:
                        # Atomic consume: UPDATE where pending and not expired, returning
                        cur.execute(
                            """
                            UPDATE login_sessions
                            SET status = 'consumed', consumed_at = NOW()
                            WHERE state_hash = %s AND status = 'pending' AND expires_at > NOW()
                            RETURNING id::text, user_id::text, provider, state_hash, pkce_verifier_encrypted,
                                      redirect_uri, requested_scopes, return_surface, return_reference,
                                      status, expires_at, created_at
                            """,
                            (state_hash,),
                        )
                        row = cur.fetchone()
                        if row is None:
                            # Check why — expired, wrong status, or not found
                            cur.execute("SELECT status, expires_at FROM login_sessions WHERE state_hash = %s", (state_hash,))
                            existing = cur.fetchone()
                            if existing:
                                status, exp = existing
                                logger.warning(
                                    "consume failed status=%s expired=%s provider=%s corr=%s",
                                    status,
                                    exp,
                                    provider,
                                    correlation,
                                )
                                if exp and exp < datetime.now(UTC):
                                    with conn.cursor() as cur2:
                                        cur2.execute("UPDATE login_sessions SET status='expired' WHERE state_hash=%s", (state_hash,))
                                        conn.commit()
                            conn.commit()
                            # Fallback to memory check if DB had no row but memory has it (from earlier fallback insert)
                            entry = self._mem_sessions.get(state_hash)
                            if entry is not None:
                                if entry["status"] != "pending":
                                    return None
                                if entry["expires_at"] < datetime.now(UTC):
                                    entry["status"] = "expired"
                                    return None
                                popped = self._mem_sessions.pop(state_hash, None)
                                if popped:
                                    popped["status"] = "consumed"
                                    return popped
                            return None
                        conn.commit()
                        cols = [
                            "id",
                            "user_id",
                            "provider",
                            "state_hash",
                            "pkce_verifier_encrypted",
                            "redirect_uri",
                            "requested_scopes",
                            "return_surface",
                            "return_reference",
                            "status",
                            "expires_at",
                            "created_at",
                        ]
                        d = dict(zip(cols, row, strict=False))
                        return d
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug("DB consume failed, trying memory: %s", exc)
                # Fall through to memory
        # Memory
        entry = self._mem_sessions.get(state_hash)
        if entry is None:
            return None
        if entry["status"] != "pending":
            return None
        if entry["expires_at"] < datetime.now(UTC):
            entry["status"] = "expired"
            return None
        popped = self._mem_sessions.pop(state_hash, None)
        if popped is None:
            return None
        popped["status"] = "consumed"
        return popped

    async def _mark_session(self, state: str, provider: str, new_status: str) -> None:
        state_hash = _hash_state(state)
        if self._db_configured():
            try:
                from nally import db

                conn = db.pooled_connect()
                try:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE login_sessions SET status=%s WHERE state_hash=%s", (new_status, state_hash))
                        conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass
        entry = self._mem_sessions.get(state_hash)
        if entry:
            entry["status"] = new_status

    async def _update_session_status(self, session_id: str, status: str) -> None:
        if self._db_configured():
            try:
                from nally import db

                conn = db.pooled_connect()
                try:
                    with conn.cursor() as cur:
                        if status == "succeeded":
                            cur.execute("UPDATE login_sessions SET status=%s, consumed_at=NOW() WHERE id=%s", (status, session_id))
                        else:
                            cur.execute("UPDATE login_sessions SET status=%s WHERE id=%s", (status, session_id))
                        conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass
        # Also update memory if present (handles fallback case)
        for entry in self._mem_sessions.values():
            if entry["id"] == session_id:
                entry["status"] = status
                break

    # ------------------------------------------------------------------
    # Polling / status helpers
    # ------------------------------------------------------------------

    async def poll_session(self, session_id: str) -> dict[str, Any] | None:
        """Poll for session completion (not provider polling)."""
        if self._db_configured():
            try:
                from nally import db

                conn = db.pooled_connect()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT id::text, user_id::text, provider, status, expires_at, return_surface, return_reference
                            FROM login_sessions WHERE id = %s
                            """,
                            (session_id,),
                        )
                        row = cur.fetchone()
                        if row is None:
                            raise ValueError("not found")
                        cols = ["id", "user_id", "provider", "status", "expires_at", "return_surface", "return_reference"]
                        d = dict(zip(cols, row, strict=False))
                        if d["expires_at"] and d["expires_at"] < datetime.now(UTC) and d["status"] == "pending":
                            d["status"] = "expired"
                        return d
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug("poll_session DB failed, trying memory: %s", exc)
        for entry in self._mem_sessions.values():
            if entry["id"] == session_id:
                return {"id": entry["id"], "user_id": entry["user_id"], "provider": entry["provider"], "status": entry["status"], "expires_at": entry["expires_at"]}
        return None

    async def revoke(self, user_id: str, provider: str) -> bool:
        """Revoke credential and invalidate cache."""
        # Try provider revoke first (best-effort)
        cred = self._vault.get(user_id, provider)
        if cred:
            try:
                provider_obj = self._get_provider(provider)
                from nally.oauth.models import OAuthToken

                token = OAuthToken(
                    provider=provider,
                    access_token=cred.access_token,
                    refresh_token=cred.refresh_token,
                    token_type=cred.token_type,
                    expires_at=cred.expires_at,
                    scopes=cred.scopes,
                    account=cred.subject,
                )
                revoke_fn = getattr(provider_obj, "revoke", None)
                if revoke_fn:
                    await revoke_fn(token)  # type: ignore
            except Exception as exc:
                logger.debug("revoke provider call failed for %s: %s", provider, exc)
        ok = self._vault.delete(user_id, provider)
        try:
            from nally.mcp.broker import get_broker

            broker = get_broker()
            await broker.invalidate_cache(user_id, provider)
        except Exception:
            pass
        logger.info("revoke provider=%s user=%s ok=%s", provider, user_id[:8], ok)
        return ok

    async def status(self, user_id: str) -> dict[str, dict[str, Any]]:
        """Return status for all known providers."""
        result: dict[str, dict[str, Any]] = {}
        for name in ("github", "gmail", "notion"):
            cred = self._vault.get(user_id, name)
            if cred and not cred.is_expired:
                result[name] = {
                    "connected": True,
                    "account": cred.provider_metadata.get("account") or cred.subject,
                    "subject": cred.subject,
                    "scopes": list(cred.scopes),
                    "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                }
            elif cred and cred.is_expired:
                result[name] = {
                    "connected": False,
                    "account": cred.provider_metadata.get("account") or cred.subject,
                    "reauth_required": True,
                    "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                }
            else:
                result[name] = {"connected": False, "account": None, "reauth_required": False}
        return result


# Singleton
_default_broker: AuthBroker | None = None


def get_broker() -> AuthBroker:
    global _default_broker
    if _default_broker is None:
        b = AuthBroker()
        b._load_builtin_providers()
        _default_broker = b
    return _default_broker


def reset_broker() -> None:
    global _default_broker
    _default_broker = None
