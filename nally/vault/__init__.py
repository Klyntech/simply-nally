"""CredentialVault — encrypted credential storage with DB + file fallback.

Single source of truth per (user_id, provider, subject).
- When DATABASE_URL is configured: store in NEON `credentials` table (encrypted bytes)
- Otherwise: file fallback at ~/.config/simply-nally/vault/{user_id}/{provider}.json
  (still envelope-encrypted if master key is set, else PLAINTEXT marker)

Isolation invariant: credential belongs to (user_id, provider, subject) and
cannot be reused across users/providers/resources. AAD binding prevents cross-use.

Transport helper: get_for_transport() returns only ephemeral headers/env, never raw tokens
to caller that logs them. But internal get() does return tokens for provider refresh/identity.

Logging: never logs token values, only provider/user_id/outcome/correlation IDs.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .crypto import decrypt, encrypt, is_encryption_configured, check_and_warn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VaultCredential:
    id: str
    user_id: str
    provider: str
    subject: str
    access_token: str
    refresh_token: str | None
    token_type: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    provider_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at


@dataclass(frozen=True)
class TransportCredential:
    """Ephemeral credential for MCP transport — headers or env, not raw tokens elsewhere."""

    provider: str
    headers: dict[str, str] | None = None
    env: dict[str, str] | None = None
    expires_at: datetime | None = None


# File fallback paths
_VAULT_FILE_BASE = Path("~/.config/simply-nally/vault").expanduser()
_LEGACY_TOKEN_BASE = Path("~/.config/simply-nally/tokens").expanduser()

# Stdio env map (same as before)
_STDIO_ENV_MAP = {
    "github": "GITHUB_PERSONAL_ACCESS_TOKEN",
    "gmail": "GMAIL_TOKEN",
    "notion": "NOTION_TOKEN",
}

_HTTP_HEADER = "Authorization"


def _aad(user_id: str, provider: str, credential_id: str) -> bytes:
    return f"{user_id}:{provider}:{credential_id}".encode()


def _parse_expires_at(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=UTC)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:
            return None
    return None


class CredentialVault:
    """Encrypted credential vault — DB-backed with file fallback."""

    def __init__(self, master_key: bytes | None = None) -> None:
        # master_key override for tests; if None we load from env lazily
        self._master_key_override = master_key
        # Validate encryption config
        if not is_encryption_configured() and master_key is None:
            check_and_warn()

    def _master(self) -> bytes | None:
        if self._master_key_override is not None:
            return self._master_key_override
        from .crypto import _load_master_key

        return _load_master_key()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(
        self,
        user_id: str,
        provider: str,
        subject: str,
        access_token: str,
        refresh_token: str | None,
        token_type: str = "Bearer",
        scopes: list[str] | None = None,
        expires_at: datetime | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> VaultCredential:
        """Atomic upsert of credential. Returns VaultCredential."""
        scopes = scopes or []
        provider_metadata = provider_metadata or {}
        now = datetime.now(UTC)
        # Try DB first with fallback to file on any DB error
        if self._db_configured():
            try:
                return self._db_put(
                    user_id, provider, subject, access_token, refresh_token, token_type, scopes, expires_at, provider_metadata, now
                )
            except Exception as exc:
                logger.warning("vault DB put failed, falling back to file: %s", exc)
        return self._file_put(
            user_id, provider, subject, access_token, refresh_token, token_type, scopes, expires_at, provider_metadata, now
        )

    def get(self, user_id: str, provider: str, subject: str | None = None) -> VaultCredential | None:
        """Get credential by user_id/provider (and optional subject). If subject not given, returns first."""
        if self._db_configured():
            try:
                cred = self._db_get(user_id, provider, subject)
                if cred is not None:
                    return cred
                # Also check file fallback for migration
                file_cred = self._file_get(user_id, provider)
                if file_cred:
                    return file_cred
                return None
            except Exception as exc:
                logger.debug("vault DB get failed, trying file: %s", exc)
        return self._file_get(user_id, provider)

    def get_valid(self, user_id: str, provider: str) -> VaultCredential | None:
        cred = self.get(user_id, provider)
        if cred is None:
            return None
        if cred.is_expired:
            logger.debug("Credential expired for %s/%s", user_id, provider)
            return None
        return cred

    def list_providers(self, user_id: str) -> list[str]:
        if self._db_configured():
            try:
                db_list = self._db_list(user_id)
                file_list = self._file_list(user_id)
                # merge unique
                return sorted(set(db_list) | set(file_list))
            except Exception as exc:
                logger.debug("vault DB list failed: %s", exc)
        return self._file_list(user_id)

    def delete(self, user_id: str, provider: str) -> bool:
        deleted = False
        if self._db_configured():
            try:
                if self._db_delete(user_id, provider):
                    deleted = True
            except Exception as exc:
                logger.debug("vault DB delete failed: %s", exc)
        # Always try file delete as well (covers fallback and migration)
        if self._file_delete(user_id, provider):
            deleted = True
        # Also try legacy file delete
        try:
            legacy = _LEGACY_TOKEN_BASE / user_id / f"{provider}.json"
            if legacy.exists():
                legacy.unlink()
                deleted = True
        except Exception:
            pass
        return deleted

    def delete_all(self, user_id: str) -> int:
        n = 0
        if self._db_configured():
            try:
                n += self._db_delete_all(user_id)
            except Exception as exc:
                logger.debug("vault DB delete_all failed: %s", exc)
        n += self._file_delete_all(user_id)
        return n

    def get_for_transport(
        self, user_id: str, provider: str, resource: str | None = None
    ) -> TransportCredential | None:
        """Return TransportCredential for MCP injection (headers or env)."""
        cred = self.get_valid(user_id, provider)
        if cred is None:
            return None
        # Enforce resource audience binding if stored
        if resource and cred.provider_metadata.get("resource") and cred.provider_metadata["resource"] != resource:
            logger.warning(
                "Credential resource mismatch for %s/%s: expected %s got %s",
                user_id,
                provider,
                cred.provider_metadata.get("resource"),
                resource,
            )
            return None
        # Decide transport: if provider_metadata says stdio, use env else headers
        # Default: http -> headers, stdio -> env based on config
        is_stdio = cred.provider_metadata.get("transport") == "stdio"
        if is_stdio:
            key = _STDIO_ENV_MAP.get(provider, f"{provider.upper()}_TOKEN")
            return TransportCredential(
                provider=provider, env={key: cred.access_token}, expires_at=cred.expires_at
            )
        else:
            return TransportCredential(
                provider=provider,
                headers={_HTTP_HEADER: f"{cred.token_type} {cred.access_token}" if cred.token_type.lower() != "bearer" else f"Bearer {cred.access_token}"},
                expires_at=cred.expires_at,
            )

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _db_configured(self) -> bool:
        try:
            from nally import db

            return db.is_configured()
        except Exception:
            return False

    def _db_put(
        self, user_id, provider, subject, access_token, refresh_token, token_type, scopes, expires_at, provider_metadata, now
    ) -> VaultCredential:
        from nally import db
        import uuid

        # Generate credential id for AAD if inserting; but we need to fetch existing first for stable id
        existing = self._db_get(user_id, provider, subject)
        cred_id = existing.id if existing else str(uuid.uuid4())
        aad = _aad(user_id, provider, cred_id)
        key = self._master()
        at_enc = encrypt(access_token.encode(), aad, key)
        rt_enc = encrypt(refresh_token.encode(), aad, key) if refresh_token else None

        conn = db.pooled_connect()
        try:
            with conn.cursor() as cur:
                # Ensure user exists? assume yes — if not, FK will fail and we propagate
                cur.execute(
                    """
                    INSERT INTO credentials (
                        id, user_id, provider, subject,
                        access_token_encrypted, refresh_token_encrypted,
                        token_type, scopes, expires_at, provider_metadata,
                        created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)
                    ON CONFLICT (user_id, provider, subject) DO UPDATE SET
                        access_token_encrypted = EXCLUDED.access_token_encrypted,
                        refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                        token_type = EXCLUDED.token_type,
                        scopes = EXCLUDED.scopes,
                        expires_at = EXCLUDED.expires_at,
                        provider_metadata = EXCLUDED.provider_metadata,
                        updated_at = NOW()
                    RETURNING id::text, user_id::text, provider, subject,
                              access_token_encrypted, refresh_token_encrypted,
                              token_type, scopes, expires_at, provider_metadata,
                              created_at, updated_at
                    """,
                    (
                        cred_id,
                        user_id,
                        provider,
                        subject,
                        bytes(at_enc),
                        bytes(rt_enc) if rt_enc else None,
                        token_type,
                        json.dumps(scopes),
                        expires_at,
                        json.dumps(provider_metadata),
                        now,
                        now,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                # Decrypt to return plain
                # row: id, user_id, provider, subject, at_enc, rt_enc, token_type, scopes, expires_at, metadata, created_at, updated_at
                return self._row_to_credential(row, key_override=key)
        finally:
            conn.close()

    def _db_get(self, user_id: str, provider: str, subject: str | None = None) -> VaultCredential | None:
        from nally import db

        conn = db.pooled_connect()
        try:
            with conn.cursor() as cur:
                if subject:
                    cur.execute(
                        """
                        SELECT id::text, user_id::text, provider, subject,
                               access_token_encrypted, refresh_token_encrypted,
                               token_type, scopes, expires_at, provider_metadata,
                               created_at, updated_at
                        FROM credentials
                        WHERE user_id = %s AND provider = %s AND subject = %s
                        """,
                        (user_id, provider, subject),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id::text, user_id::text, provider, subject,
                               access_token_encrypted, refresh_token_encrypted,
                               token_type, scopes, expires_at, provider_metadata,
                               created_at, updated_at
                        FROM credentials
                        WHERE user_id = %s AND provider = %s
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        (user_id, provider),
                    )
                row = cur.fetchone()
                if row is None:
                    return None
                return self._row_to_credential(row)
        finally:
            conn.close()

    def _db_list(self, user_id: str) -> list[str]:
        from nally import db

        conn = db.pooled_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT provider FROM credentials WHERE user_id = %s", (user_id,))
                rows = cur.fetchall()
                return [r[0] for r in rows]
        finally:
            conn.close()

    def _db_delete(self, user_id: str, provider: str) -> bool:
        from nally import db

        conn = db.pooled_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM credentials WHERE user_id = %s AND provider = %s", (user_id, provider))
                ok = cur.rowcount > 0
                conn.commit()
                return bool(ok)
        finally:
            conn.close()

    def _db_delete_all(self, user_id: str) -> int:
        from nally import db

        conn = db.pooled_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM credentials WHERE user_id = %s", (user_id,))
                n = cur.rowcount
                conn.commit()
                return int(n)
        finally:
            conn.close()

    def _row_to_credential(self, row, key_override: bytes | None = None) -> VaultCredential:
        (
            cid,
            user_id,
            provider,
            subject,
            at_enc,
            rt_enc,
            token_type,
            scopes_json,
            expires_at,
            metadata_json,
            created_at,
            updated_at,
        ) = row
        key = key_override if key_override is not None else self._master()
        aad = _aad(user_id, provider, cid)
        # at_enc/rt_enc are bytes or memoryview
        if isinstance(at_enc, memoryview):
            at_enc = at_enc.tobytes()
        if isinstance(rt_enc, memoryview):
            rt_enc = rt_enc.tobytes()
        at_plain = decrypt(bytes(at_enc), aad, key).decode()
        rt_plain = None
        if rt_enc is not None:
            rt_plain = decrypt(bytes(rt_enc), aad, key).decode()
        # scopes/metadata may be dict/list or string
        if isinstance(scopes_json, str):
            try:
                scopes = json.loads(scopes_json)
            except Exception:
                scopes = []
        else:
            scopes = scopes_json or []
        if isinstance(metadata_json, str):
            try:
                metadata = json.loads(metadata_json)
            except Exception:
                metadata = {}
        else:
            metadata = metadata_json or {}
        # times are already datetime, ensure UTC
        def _ensure_utc(dt):
            if dt is None:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt

        return VaultCredential(
            id=cid,
            user_id=user_id,
            provider=provider,
            subject=subject,
            access_token=at_plain,
            refresh_token=rt_plain,
            token_type=token_type or "Bearer",
            scopes=tuple(scopes) if isinstance(scopes, list) else (),
            expires_at=_ensure_utc(expires_at),
            provider_metadata=metadata if isinstance(metadata, dict) else {},
            created_at=_ensure_utc(created_at) or datetime.now(UTC),
            updated_at=_ensure_utc(updated_at) or datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # File fallback (encrypted as well)
    # ------------------------------------------------------------------

    def _file_put(self, user_id, provider, subject, access_token, refresh_token, token_type, scopes, expires_at, provider_metadata, now) -> VaultCredential:
        import uuid

        base = _VAULT_FILE_BASE / user_id
        base.mkdir(parents=True, exist_ok=True)
        p = base / f"{provider}.json"
        # Use stable id from existing file if present
        cred_id = None
        if p.exists():
            try:
                existing_data = json.loads(p.read_text(encoding="utf-8"))
                cred_id = existing_data.get("id")
            except Exception:
                pass
        if not cred_id:
            cred_id = str(uuid.uuid4())
        aad = _aad(user_id, provider, cred_id)
        key = self._master()
        at_enc = encrypt(access_token.encode(), aad, key)
        rt_enc = encrypt(refresh_token.encode(), aad, key) if refresh_token else None
        # Store as base64 for JSON
        import base64

        at_b64 = base64.b64encode(at_enc).decode()
        rt_b64 = base64.b64encode(rt_enc).decode() if rt_enc else None
        data = {
            "id": cred_id,
            "user_id": user_id,
            "provider": provider,
            "subject": subject,
            "access_token_encrypted": at_b64,
            "refresh_token_encrypted": rt_b64,
            "token_type": token_type,
            "scopes": scopes,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "provider_metadata": provider_metadata,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        return VaultCredential(
            id=cred_id,
            user_id=user_id,
            provider=provider,
            subject=subject,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            scopes=tuple(scopes),
            expires_at=expires_at,
            provider_metadata=provider_metadata,
            created_at=now,
            updated_at=now,
        )

    def _file_get(self, user_id: str, provider: str) -> VaultCredential | None:
        p = _VAULT_FILE_BASE / user_id / f"{provider}.json"
        if not p.exists():
            # Try legacy fallback for migration (plaintext tokens)
            legacy = _LEGACY_TOKEN_BASE / user_id / f"{provider}.json"
            if legacy.exists():
                try:
                    data = json.loads(legacy.read_text(encoding="utf-8"))
                    at = data.get("access_token")
                    if at:
                        # Convert legacy to vault format on read (do not persist automatically)
                        return VaultCredential(
                            id="legacy-" + provider,
                            user_id=user_id,
                            provider=provider,
                            subject=data.get("account") or data.get("subject") or "legacy",
                            access_token=at,
                            refresh_token=data.get("refresh_token"),
                            token_type=data.get("token_type", "Bearer"),
                            scopes=tuple(data.get("scopes", [])),
                            expires_at=_parse_expires_at(data.get("expires_at")),
                            provider_metadata={},
                            created_at=datetime.now(UTC),
                            updated_at=datetime.now(UTC),
                        )
                except Exception:
                    pass
            return None
        try:
            import base64

            data = json.loads(p.read_text(encoding="utf-8"))
            cid = data["id"]
            aad = _aad(user_id, provider, cid)
            key = self._master()
            at_enc = base64.b64decode(data["access_token_encrypted"])
            at = decrypt(at_enc, aad, key).decode()
            rt = None
            if data.get("refresh_token_encrypted"):
                rt_enc = base64.b64decode(data["refresh_token_encrypted"])
                rt = decrypt(rt_enc, aad, key).decode()
            return VaultCredential(
                id=cid,
                user_id=user_id,
                provider=provider,
                subject=data.get("subject", "unknown"),
                access_token=at,
                refresh_token=rt,
                token_type=data.get("token_type", "Bearer"),
                scopes=tuple(data.get("scopes", [])),
                expires_at=_parse_expires_at(data.get("expires_at")),
                provider_metadata=data.get("provider_metadata", {}),
                created_at=_parse_expires_at(data.get("created_at")) or datetime.now(UTC),
                updated_at=_parse_expires_at(data.get("updated_at")) or datetime.now(UTC),
            )
        except Exception as exc:
            logger.warning("Vault file read failed for %s/%s: %s", user_id, provider, exc)
            return None

    def _file_list(self, user_id: str) -> list[str]:
        d = _VAULT_FILE_BASE / user_id
        if not d.exists():
            return []
        return [f.stem for f in d.glob("*.json") if f.stem]

    def _file_delete(self, user_id: str, provider: str) -> bool:
        p = _VAULT_FILE_BASE / user_id / f"{provider}.json"
        try:
            if p.exists():
                p.unlink()
                return True
            return False
        except OSError:
            return False

    def _file_delete_all(self, user_id: str) -> int:
        d = _VAULT_FILE_BASE / user_id
        if not d.exists():
            return 0
        count = 0
        for f in d.glob("*.json"):
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
        return count


# Singleton for convenience
_default_vault: CredentialVault | None = None


def get_vault() -> CredentialVault:
    global _default_vault
    if _default_vault is None:
        _default_vault = CredentialVault()
    return _default_vault


def reset_vault() -> None:
    global _default_vault
    _default_vault = None
