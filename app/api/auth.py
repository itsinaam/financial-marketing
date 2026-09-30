import logging
from datetime import timedelta
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import deps
from app.core import security
from app.core.config import settings
from app.models.companies import Company, Role
from app.schemas.token import Token
from app.schemas.companies import (
    ForgotPasswordRequest,
    SignupRequest,
    UserLogin,
    UserResponse,
)
from app.services.referral_service import (
    ReferralServiceError,
    build_password_reset_link,
    send_password_reset_email,
)

router = APIRouter()
logger = logging.getLogger(__name__)
REFERRAL_SIGNUP_REWARD_CREDITS = 10

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

    referrer = None
    if signup_data.referrer_id is not None:
        referrer = db.query(Company).filter(
            Company.id == signup_data.referrer_id,
            Company.role == Role.COMPANY,
        ).first()
        if not referrer:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The referral link is invalid.",
            )

    company = Company(
        email=signup_data.email,
        hashed_password=security.get_password_hash(signup_data.password),
        name=signup_data.full_name,
        role=Role.COMPANY,
        is_active=True,
        is_superuser=False,
        referred_by_company_id=referrer.id if referrer else None,
    )
    db.add(company)
    if referrer:
        referrer.referral_credits += REFERRAL_SIGNUP_REWARD_CREDITS
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

@router.post("/forgot-password", summary="Request a password reset")
def forgot_password(
    request: ForgotPasswordRequest,
    db: Session = Depends(deps.get_db),
) -> dict[str, str]:
    user = db.query(Company).filter(Company.email == request.email).first()
    if user and user.is_active:
        updated = db.query(Company).filter(
            Company.id == user.id,
            Company.is_active.is_(True),
        ).update(
            {
                Company.password_reset_token_version:
                    Company.password_reset_token_version + 1
            },
            synchronize_session=False,
        )
        db.commit()
        if updated:
            db.refresh(user)
            token = security.create_password_reset_token(
                user.email, user.password_reset_token_version
            )
            try:
                reset_link = build_password_reset_link(token)
                send_password_reset_email(user.email, reset_link)
            except ReferralServiceError:
                logger.exception("Failed to send a password reset email")

    return {"message": "If an account exists for that email, a reset link has been sent."}

@router.get("/me", response_model=UserResponse, summary="Get current logged in user details")
def read_user_me(
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Get profile details for currently authenticated user.
    """
    return current_user
