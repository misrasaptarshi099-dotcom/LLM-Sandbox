"""Unit tests for cryptographic security module."""

from __future__ import annotations

import pytest

from app.core.security import (
    decrypt_system_prompt,
    encrypt_system_prompt,
    hash_text,
    verify_auth_token,
)


def test_encryption_and_decryption_roundtrip() -> None:
    secret_prompt = "You are a vault keeper. Flag is TECHNOVIT{secret}."
    ciphertext = encrypt_system_prompt(secret_prompt)

    # Ciphertext must not equal plaintext
    assert ciphertext != secret_prompt
    assert ":" in ciphertext

    # Decryption recovers exact plaintext
    decrypted = decrypt_system_prompt(ciphertext)
    assert decrypted == secret_prompt


def test_tampered_ciphertext_fails_decryption() -> None:
    secret_prompt = "Super secret instructions"
    ciphertext = encrypt_system_prompt(secret_prompt)

    # Corrupt ciphertext
    parts = ciphertext.split(":")
    corrupted = f"{parts[0]}:corrupted_payload"

    with pytest.raises(ValueError, match="Failed to decrypt"):
        decrypt_system_prompt(corrupted)


def test_hash_text_deterministic() -> None:
    text = "Hello LLM Challenge"
    hash1 = hash_text(text)
    hash2 = hash_text(text)
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex string


def test_verify_auth_token() -> None:
    assert verify_auth_token("dev-token") is True
    assert verify_auth_token("Bearer dev-token") is True
    assert verify_auth_token("invalid-token") is False
