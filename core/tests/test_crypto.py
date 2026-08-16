import os

import pytest

from core import crypto
from core.crypto import CredentialKeyError, decrypt_secret, encrypt_secret


def test_secret_round_trip_uses_private_key_file(settings, tmp_path):
    settings.TICKETWATCH_KEY_FILE = tmp_path / ".ticketwatch.key"

    token = encrypt_secret("authorization-code")

    assert token != "authorization-code"
    assert decrypt_secret(token) == "authorization-code"
    assert os.stat(settings.TICKETWATCH_KEY_FILE).st_mode & 0o777 == 0o600


def test_missing_key_does_not_replace_existing_ciphertext(settings, tmp_path):
    settings.TICKETWATCH_KEY_FILE = tmp_path / ".ticketwatch.key"
    token = encrypt_secret("authorization-code")
    settings.TICKETWATCH_KEY_FILE.unlink()

    with pytest.raises(CredentialKeyError, match="missing"):
        decrypt_secret(token)


def test_windows_rejects_encryption_before_creating_a_key(settings, tmp_path, monkeypatch):
    key_path = tmp_path / ".ticketwatch.key"
    settings.TICKETWATCH_KEY_FILE = key_path
    monkeypatch.setattr(crypto, "_supports_private_key_permissions", lambda: False, raising=False)

    with pytest.raises(CredentialKeyError, match="Windows"):
        encrypt_secret("authorization-code")

    assert not key_path.exists()


def test_windows_rejects_decryption_before_reading_an_existing_key(settings, tmp_path, monkeypatch):
    key_path = tmp_path / ".ticketwatch.key"
    key_path.write_bytes(b"not a valid Fernet key")
    settings.TICKETWATCH_KEY_FILE = key_path
    monkeypatch.setattr(crypto, "_supports_private_key_permissions", lambda: False, raising=False)

    with pytest.raises(CredentialKeyError, match="Windows"):
        decrypt_secret("invalid-ciphertext")

    assert key_path.read_bytes() == b"not a valid Fernet key"


def test_malformed_local_key_is_a_credential_error_during_encryption(settings, tmp_path):
    """Leaking Fernet's raw malformed-key ValueError from encryption must fail this test."""
    settings.TICKETWATCH_KEY_FILE = tmp_path / ".ticketwatch.key"
    settings.TICKETWATCH_KEY_FILE.write_bytes(b"truncated-local-key")

    with pytest.raises(CredentialKeyError, match="invalid"):
        encrypt_secret("authorization-code")


def test_malformed_local_key_is_a_credential_error_during_decryption(settings, tmp_path):
    """Leaking Fernet's raw malformed-key ValueError from decryption must fail this test."""
    settings.TICKETWATCH_KEY_FILE = tmp_path / ".ticketwatch.key"
    settings.TICKETWATCH_KEY_FILE.write_bytes(b"truncated-local-key")

    with pytest.raises(CredentialKeyError, match="invalid"):
        decrypt_secret("ciphertext")
