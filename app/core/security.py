"""Security, Encryption, and Authentication utilities.

Rules §2, PRD §7, §11:
- AES-256-GCM encryption for system prompts at rest.
- System prompt is decrypted only inside worker execution, never in public APIs.
- SHA-256 hashing for prompt deduplication and integrity.
- Redacted token verification.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

# Nonce size for AES-GCM is standard 12 bytes (96 bits)
NONCE_SIZE_BYTES: Final[int] = 12


def _get_key_bytes(secret: str | bytes | None = None) -> bytes:
    """Normalize secret string to a 32-byte AES key."""
    raw_secret = secret or get_settings().aes_256_gcm_secret
    if isinstance(raw_secret, bytes):
        if len(raw_secret) == 32:
            return raw_secret
        raw_secret = raw_secret.decode("utf-8")

    # If it's a 64-char hex string, decode hex
    if len(raw_secret) == 64:
        try:
            key = bytes.fromhex(raw_secret)
            if len(key) == 32:
                return key
        except ValueError:
            pass

    # Fallback to SHA-256 hash of the secret string to guarantee 32 bytes
    return hashlib.sha256(raw_secret.encode("utf-8")).digest()


def hash_text(text: str) -> str:
    """Return SHA-256 hex digest of given text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encrypt_system_prompt(plaintext: str, secret_key: str | bytes | None = None) -> str:
    """Encrypt system prompt using AES-256-GCM.

    Returns encoded string format: 'nonce_b64:ciphertext_b64'
    """
    key = _get_key_bytes(secret_key)
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_SIZE_BYTES)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")
    return f"{nonce_b64}:{ciphertext_b64}"


def decrypt_system_prompt(payload: str, secret_key: str | bytes | None = None) -> str:
    """Decrypt an AES-256-GCM encrypted system prompt payload."""
    try:
        parts = payload.split(":", 1)
        if len(parts) != 2:
            raise ValueError("Invalid encrypted payload format")

        nonce_b64, ciphertext_b64 = parts
        nonce = base64.b64decode(nonce_b64.encode("ascii"))
        ciphertext = base64.b64decode(ciphertext_b64.encode("ascii"))

        key = _get_key_bytes(secret_key)
        aesgcm = AESGCM(key)
        decrypted_bytes = aesgcm.decrypt(nonce, ciphertext, None)
        return decrypted_bytes.decode("utf-8")
    except Exception as exc:
        msg = "Failed to decrypt system prompt — invalid key or corrupted ciphertext"
        raise ValueError(msg) from exc


def verify_auth_token(token: str) -> bool:
    """Verify development bearer token."""
    expected = get_settings().dev_auth_token
    return token == expected or token == f"Bearer {expected}"
