from typing import Generator
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.companies import Company, Role
from app.schemas.token import TokenPayload

security_scheme = HTTPBearer()

def get_db() -> Generator:
    """Yield DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(auth: HTTPAuthorizationCredentials = Depends(security_scheme), db: Session = Depends(get_db)) -> Company:
    """
    Validate access token and return current authenticated user.
    """
    token = auth.credentials
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
    except (JWTError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not token_data.sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    user = db.query(Company).filter(Company.email == token_data.sub).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )
    return user

def require_roles(*allowed_roles: Role):
    """
    Reusable dependency factory to validate that current user has one of the allowed roles.
    Superusers always bypass this restriction.
    """     
    def role_checker(current_user: Company = Depends(get_current_user)) -> Company:
        if current_user.role not in allowed_roles and not current_user.is_superuser:
            role_names = " or ".join(r.value.replace("_", " ").title() for r in allowed_roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"The user does not have enough privileges ({role_names} required)",
            )
        return current_user
    return role_checker

# Reusable role-based dependencies
get_current_superadmin = require_roles(Role.SUPERADMIN)


