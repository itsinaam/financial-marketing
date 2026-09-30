from datetime import datetime, timedelta, timezone
from typing import Any, Union
from jose import jwt
from passlib.context import CryptContext
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = 30

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain password against hashed password."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Hash password using bcrypt."""
    return pwd_context.hash(password)

def create_access_token(
    subject: Union[str, Any], expires_delta: timedelta | None = None
) -> str:
    """Generate JWT Access Token."""
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode = {"exp": expire, "sub": str(subject)}
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt

def create_password_reset_token(subject: str, token_version: int) -> str:
    """Generate a short-lived token that can only be used to reset a password."""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(
        {
            "exp": expire,
            "sub": subject,
            "purpose": "password_reset",
            "version": token_version,
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )

