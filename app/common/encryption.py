"""AES-256-GCM symmetric encryption matching apps/api/src/common/utils/encryption.util.ts.

Layout (base64-encoded):  iv(12) | tag(16) | ciphertext(N)

Compatible byte-for-byte with the Node implementation so values stored by the
Nest API can be decrypted from Python and vice versa.
"""
from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_LEN = 12
_TAG_LEN = 16


def _get_key() -> bytes:
    hex_key = os.environ.get("MASTER_ENCRYPTION_KEY")
    if not hex_key:
        # Fallback to settings (pydantic-settings loads .env but doesn't push to os.environ).
        try:
            from ..config import get_settings  # local import avoids circular at module load
            hex_key = get_settings().MASTER_ENCRYPTION_KEY
        except Exception:
            hex_key = None
    if not hex_key or len(hex_key) != 64:
        raise RuntimeError("MASTER_ENCRYPTION_KEY must be 64 hex characters (32 bytes)")
    return bytes.fromhex(hex_key)


def encrypt(plaintext: str) -> str:
    """Encrypt plaintext, returning base64(iv | tag | ciphertext)."""
    key = _get_key()
    iv = secrets.token_bytes(_IV_LEN)
    aes = AESGCM(key)
    ct_and_tag = aes.encrypt(iv, plaintext.encode("utf-8"), None)
    # cryptography returns ciphertext|tag; reorder to iv|tag|ct to match Node.
    ct, tag = ct_and_tag[:-_TAG_LEN], ct_and_tag[-_TAG_LEN:]
    return base64.b64encode(iv + tag + ct).decode("ascii")


def decrypt(ciphertext_b64: str) -> str:
    """Decrypt a base64(iv | tag | ciphertext) payload."""
    blob = base64.b64decode(ciphertext_b64)
    iv = blob[:_IV_LEN]
    tag = blob[_IV_LEN : _IV_LEN + _TAG_LEN]
    ct = blob[_IV_LEN + _TAG_LEN :]
    aes = AESGCM(_get_key())
    plain = aes.decrypt(iv, ct + tag, None)
    return plain.decode("utf-8")


def mask_secret(secret: str) -> str:
    """Mask for UI display: keep first 3 + last 3 chars, otherwise ``***``."""
    if not secret or len(secret) <= 8:
        return "***"
    return f"{secret[:3]}{'*' * (len(secret) - 6)}{secret[-3:]}"


def decrypt_db_password(stored: str) -> str:
    """Tenant DB passwords may be stored as ``PLAINTEXT:<raw>`` in dev."""
    if not stored:
        raise RuntimeError(
            "Tenant has no DB password set (dbPasswordEnc is empty). "
            "Run the provisioning task (Celery 'tenant.provision') to set up the per-tenant DB."
        )
    if stored.startswith("PLAINTEXT:"):
        return stored[len("PLAINTEXT:") :]
    return decrypt(stored)
