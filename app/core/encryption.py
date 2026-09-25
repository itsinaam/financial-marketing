from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings


def _fernet() -> Fernet:
    key = settings.CREDENTIALS_ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    if not value:
        return value
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        # Value was stored before encryption was introduced (plaintext).
        return value