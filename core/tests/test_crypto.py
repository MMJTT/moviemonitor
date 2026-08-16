import os

import pytest

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
