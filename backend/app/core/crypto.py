"""Symmetric encryption for user secrets (LLM / SSH keys).

Values are encrypted at rest with Fernet and never returned to the UI in
plaintext. The key comes from SECRET_ENCRYPTION_KEY.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    return Fernet(settings.secret_encryption_key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - defensive
        raise ValueError("Could not decrypt secret: invalid key or token") from exc
