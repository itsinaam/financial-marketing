from datetime import timedelta
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import deps
from app.core import security
from app.core.config import settings
from app.models.companies import Company, Role
from app.schemas.token import Token
from app.schemas.companies import UserResponse, UserLogin, SignupRequest

router = APIRouter()

@router.post("/signup", response_model=Token, status_code=status.HTTP_201_CREATED, summary="Public company signup")
def signup(
    signup_data: SignupRequest,
    db: Session = Depends(deps.get_db)
) -> Any:
    """
    Public signup endpoint. Creates a new Company account and returns an access token.
    """
    existing_user = db.query(Company).filter(Company.email == signup_data.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email already exists.",
        )

    company = Company(
        email=signup_data.email,
        hashed_password=security.get_password_hash(signup_data.password),
        name=signup_data.full_name,
        role=Role.COMPANY,
        is_active=True,
        is_superuser=False,
    )
    db.add(company)
    db.commit()
    db.refresh(company)

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        "access_token": security.create_access_token(
            subject=company.email, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
    }

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
