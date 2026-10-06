import logging
from datetime import timedelta
from typing import Any
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.core import deps
from app.core import security
from app.core.config import settings
from app.models.companies import Company, Role
from app.models.knowledge_base import KnowledgeBaseItem, KnowledgeBaseSource
from app.models.referral import ReferralInvite
from app.schemas.auth import WebsiteScrapeStatusResponse
from app.schemas.token import Token
from app.schemas.companies import (
    ForgotPasswordRequest,
    SignupRequest,
    UserLogin,
    UserResponse,
)
from app.services.referral_service import (
    REFERRAL_REWARD_CREDITS,
    ReferralServiceError,
    build_password_reset_link,
    send_password_reset_email,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _scrape_signup_website(company_id: int, website_url: str) -> None:
    from app.core.database import SessionLocal
    from app.services.knowledge_base_service import store_scraped_site

    db = SessionLocal()
    try:
        company = db.query(Company).filter(Company.id == company_id).first()
        if not company:
            return

        company.website_scrape_status = "running"
        company.website_scrape_error = None
        db.commit()

        store_scraped_site(db, company, website_url, title=f"Company website for {company.name or company.email}")
        company.website = website_url
        company.website_scrape_status = "completed"
        company.website_scrape_error = None
        db.commit()
    except Exception as exc:
        company = db.query(Company).filter(Company.id == company_id).first()
        if company:
            company.website_scrape_status = "failed"
            company.website_scrape_error = str(exc)[:1000]
            db.commit()
        logger.exception("Signup website scrape failed for company %s", company_id)
    finally:
        db.close()


@router.get("/website-scrape-status", response_model=WebsiteScrapeStatusResponse, summary="Check the current signup website scrape status")
def get_signup_website_scrape_status(
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    status_value = current_user.website_scrape_status or "not_started"
    latest_item = (
        db.query(KnowledgeBaseItem)
        .filter(KnowledgeBaseItem.company_id == current_user.id)
        .filter(KnowledgeBaseItem.source_type == KnowledgeBaseSource.WEBSITE)
        .order_by(KnowledgeBaseItem.created_at.desc())
        .first()
    )
    download_url = latest_item.source_url if latest_item else None
    message = None
    if status_value == "not_started":
        message = "No website scrape has started yet."
    elif status_value == "running":
        message = "Website scraping is in progress."
    elif status_value == "completed":
        message = "Website scraping completed successfully."
    elif status_value == "failed":
        message = "Website scraping failed."

    return WebsiteScrapeStatusResponse(
        status=status_value,
        website=current_user.website,
        download_url=download_url,
        message=message,
        error=current_user.website_scrape_error,
    )

@router.post("/signup", response_model=Token, status_code=status.HTTP_201_CREATED, summary="Public company signup")
def signup(
    signup_data: SignupRequest,
    background: BackgroundTasks,
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
        website=signup_data.website.strip() if signup_data.website else None,
        website_scrape_status="not_started" if signup_data.website else "not_required",
        role=Role.COMPANY,
        is_active=True,
        is_superuser=False,
        referred_by_company_id=referrer.id if referrer else None,
    )
    db.add(company)
    if referrer:
        referrer.referral_credits += REFERRAL_REWARD_CREDITS
        invite = (
            db.query(ReferralInvite)
            .filter(
                ReferralInvite.referrer_company_id == referrer.id,
                ReferralInvite.invited_email == signup_data.email.strip().lower(),
            )
            .first()
        )
        if invite is None:
            invite = ReferralInvite(
                referrer_company_id=referrer.id,
                invited_email=signup_data.email.strip().lower(),
                source="link",
            )
            db.add(invite)
        invite.joined_company_id = company.id
        invite.joined_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(company)

    if signup_data.website:
        company.website_scrape_status = "queued"
        db.commit()
        background.add_task(_scrape_signup_website, company.id, signup_data.website.strip())

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
