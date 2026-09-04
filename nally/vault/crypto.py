"""CredentialVault crypto — envelope encryption with AAD binding.

Master key from NALLY_VAULT_MASTER_KEY (base64 32-byte or raw string).
Per-record key derived via HKDF? For v1 we use master directly with
AESGCM + random nonce + AAD = (user_id, provider, credential_id).

If master key is missing, we fall back to plaintext with startup warning
(explicitly temporary, for dev only).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets

logger = logging.getLogger(__name__)


def _load_master_key() -> bytes | None:
    raw = os.getenv("NALLY_VAULT_MASTER_KEY", "").strip()
    if not raw:
        # also check legacy var
        raw = os.getenv("NALLY_MASTER_KEY", "").strip()
    if not raw:
        return None
    # Try base64 urlsafe decode
    for variant in (raw, raw + "=" * (-len(raw) % 4)):
        try:
            decoded = base64.urlsafe_b64decode(variant)
            if len(decoded) == 32:
                return decoded
        except Exception:
            continue
    # Try standard base64
    try:
        decoded = base64.b64decode(raw)
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    # Try hex
    try:
        decoded = bytes.fromhex(raw)
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    # Fallback: derive 32-byte key via SHA256 of raw string
    # This is weaker but ensures dev setups work without manual key gen
    logger.warning(
        "NALLY_VAULT_MASTER_KEY is not 32-byte base64/hex — deriving via SHA256 (dev only)"
    )
    return hashlib.sha256(raw.encode()).digest()


def _get_aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise RuntimeError("cryptography not installed: pip install cryptography") from exc
    return AESGCM(key)


def encrypt(plaintext: bytes, aad: bytes, key: bytes | None = None) -> bytes:
    """Encrypt with AESGCM, prepending 12-byte nonce. Returns nonce+ciphertext."""
    if key is None:
        key = _load_master_key()
    if key is None:
        # No key — return plaintext with marker (DEFERRED encryption)
        # Prefix with b"PLAIN:" so decrypt knows
        return b"PLAIN:" + plaintext
    if len(key) != 32:
        raise ValueError("Master key must be 32 bytes")
    aesgcm = _get_aesgcm(key)
    nonce = secrets.token_bytes(12)
    ct = aesgcm.encrypt(nonce, plaintext, aad if aad else None)
    return nonce + ct


def decrypt(blob: bytes, aad: bytes, key: bytes | None = None) -> bytes:
    """Decrypt nonce+ciphertext blob."""
    if key is None:
        key = _load_master_key()
    if blob.startswith(b"PLAIN:"):
        if key is not None:
            logger.warning("Decrypting plaintext blob while master key is configured — re-encrypt needed")
        return blob[len(b"PLAIN:") :]
    if key is None:
        raise RuntimeError(
            "Credential is encrypted but NALLY_VAULT_MASTER_KEY not set — cannot decrypt. "
            "Set the same master key used at encryption time."
        )
    if len(key) != 32:
        raise ValueError("Master key must be 32 bytes")
    if len(blob) < 13:
        raise ValueError("Ciphertext too short")
    nonce = blob[:12]
    ct = blob[12:]
    aesgcm = _get_aesgcm(key)
    return aesgcm.decrypt(nonce, ct, aad if aad else None)


def generate_master_key() -> str:
    """Generate a new 32-byte master key as base64 urlsafe (for .env)."""
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def is_encryption_configured() -> bool:
    return _load_master_key() is not None


def check_and_warn() -> None:
    if not is_encryption_configured():
        logger.warning(
            "NALLY_VAULT_MASTER_KEY not set — credentials stored with plaintext marker. "
            "Set a 32-byte base64 master key for envelope encryption (run: python -m nally.vault.crypto)"
        )


if __name__ == "__main__":
    print(generate_master_key())
