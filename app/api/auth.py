from datetime import timedelta
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import deps
from app.core import security
from app.core.config import settings
from app.models.companies import Company
from app.schemas.token import Token
from app.schemas.companies import UserResponse, UserLogin

router = APIRouter()

@router.post("/login", response_model=Token, summary="JSON payload login endpoint")
def login_json(
    login_data: UserLogin,
    db: Session = Depends(deps.get_db)
) -> Any:
    """
    JSON payload login endpoint.
    """
    user = db.query(Company).filter(Company.email == login_data.email).first()
    if not user or not security.verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect email or password"
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        "access_token": security.create_access_token(
            subject=user.email, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
    }

@router.get("/me", response_model=UserResponse, summary="Get current logged in user details")
def read_user_me(
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Get profile details for currently authenticated user.
    """
    return current_user
