import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CredentialKeyError(RuntimeError):
    pass


def _supports_private_key_permissions():
    return os.name != "nt"


def _load_key(create):
    if not _supports_private_key_permissions():
        raise CredentialKeyError("credential key storage is not supported on Windows")

    path = Path(settings.TICKETWATCH_KEY_FILE)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise CredentialKeyError("credential key is missing") from None

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    return key


def _fernet(create):
    try:
        return Fernet(_load_key(create=create))
    except ValueError as exc:
        raise CredentialKeyError("credential key is invalid") from exc


def encrypt_secret(value):
    return _fernet(create=True).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token):
    try:
        return _fernet(create=False).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialKeyError("credential cannot be decrypted with the local key") from exc
