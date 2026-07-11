import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from harness.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    """Derive a valid Fernet key from settings.secret_key.

    Fernet requires a 32-byte urlsafe-base64 key; settings.secret_key is an
    arbitrary operator-chosen string (decisions.md Q16), so it's hashed down
    to the right shape rather than requiring a specifically-formatted secret.
    """
    settings = get_settings()
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt(value: bytes) -> str:
    return _fernet().decrypt(value).decode()
