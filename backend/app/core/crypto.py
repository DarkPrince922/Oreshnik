"""Symmetric encryption for user secrets (LLM / SSH keys).

Values are encrypted at rest with Fernet and never returned to the UI in
plaintext. The key comes from SECRET_ENCRYPTION_KEY.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    try:
        return Fernet(settings.secret_encryption_key.encode())
    except ValueError as exc:
        raise RuntimeError(
            "SECRET_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ) from exc


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - defensive
        raise ValueError("Could not decrypt secret: invalid key or token") from exc
