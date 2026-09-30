from datetime import datetime, timezone
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import deps
from app.core import security
from app.models.companies import Company,Role
from app.models.referral import ReferralInvite
from app.schemas.companies import UserCreate, UserResponse, UserDeleteResponse
from app.schemas.referrals import (
    ReferralEmailRequest,
    ReferralEmailResponse,
    ReferralLinkResponse,
    ReferralStatsResponse,
)
from app.services.referral_service import (
    REFERRAL_REWARD_CREDITS,
    ReferralServiceError,
    build_referral_link,
    send_referral_email,
)

router = APIRouter()

@router.get(
    "/",
    response_model=List[UserResponse],
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="List all companies (Admin or Super Admin)"
)
def read_companies(
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    Retrieve all companies. Admin or Super Admin privilege required.
    """
    companies = db.query(Company).offset(skip).limit(limit).all()
    return companies

@router.post(
    "/",
    response_model=UserResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    status_code=status.HTTP_201_CREATED,
    summary="Create company (Super Admin only)"
)
def create_company(
    *,
    db: Session = Depends(deps.get_db),
    user_in: UserCreate,
) -> Any:
    """
    Create a new company. Super Admin privilege required.
    """
    existing_user = db.query(Company).filter(Company.email == user_in.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company/User with this email already exists.",
        )
    company_name = user_in.name or user_in.full_name or "Company"
    company = Company(
        email=user_in.email,
        hashed_password=security.get_password_hash(user_in.password),
        name=company_name,
        role=user_in.role,
        is_active=user_in.is_active,
        is_superuser=(user_in.role == Role.SUPERADMIN),
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return company

@router.get(
    "/referral-link",
    response_model=ReferralLinkResponse,
    summary="Get the logged-in company's referral link",
)
def get_referral_link(
    current_user: Company = Depends(deps.get_current_user),
) -> ReferralLinkResponse:
    if current_user.role != Role.COMPANY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only companies can create referral links.",
        )
    try:
        return ReferralLinkResponse(referral_link=build_referral_link(current_user.id))
    except ReferralServiceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post(
    "/referrals/send",
    response_model=ReferralEmailResponse,
    summary="Email the company's referral link",
)
def send_referral(
    payload: ReferralEmailRequest,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> ReferralEmailResponse:
    if current_user.role != Role.COMPANY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only companies can send referral invitations.",
        )
    try:
        link = build_referral_link(current_user.id)
        invited_email = str(payload.email).strip().lower()
        if invited_email == current_user.email.strip().lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot invite your own email address.",
            )
        invite = (
            db.query(ReferralInvite)
            .filter(
                ReferralInvite.referrer_company_id == current_user.id,
                ReferralInvite.invited_email == invited_email,
            )
            .first()
        )
        if invite and invite.joined_company_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This person has already joined through your referral.",
            )
        send_referral_email(invited_email, current_user.name, link)
        if invite is None:
            invite = ReferralInvite(
                referrer_company_id=current_user.id,
                invited_email=invited_email,
                source="email",
            )
            db.add(invite)
        invite.source = "email"
        invite.sent_at = datetime.now(timezone.utc)
        db.commit()
    except ReferralServiceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return ReferralEmailResponse(referral_link=link)


@router.get(
    "/referrals",
    response_model=ReferralStatsResponse,
    summary="Get the logged-in company's referral status and invite counts",
)
def get_referral_stats(
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> ReferralStatsResponse:
    if current_user.role != Role.COMPANY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only companies can view referral statistics.",
        )

    invites = (
        db.query(ReferralInvite)
        .filter(ReferralInvite.referrer_company_id == current_user.id)
        .order_by(ReferralInvite.created_at.desc(), ReferralInvite.id.desc())
        .all()
    )
    invite_items = [
        {
            "invited_email": invite.invited_email,
            "status": "joined" if invite.joined_company_id is not None else "pending",
            "source": invite.source,
            "credits": REFERRAL_REWARD_CREDITS if invite.joined_company_id is not None else 0,
            "sent_at": invite.sent_at,
            "joined_at": invite.joined_at,
        }
        for invite in invites
    ]
    known_emails = {invite.invited_email.strip().lower() for invite in invites}
    referred_companies = (
        db.query(Company)
        .filter(Company.referred_by_company_id == current_user.id)
        .all()
    )
    for referred_company in referred_companies:
        invited_email = referred_company.email.strip().lower()
        if invited_email in known_emails:
            continue
        known_emails.add(invited_email)
        invite_items.append(
            {
                "invited_email": invited_email,
                "status": "joined",
                "source": "link",
                "credits": REFERRAL_REWARD_CREDITS,
                "sent_at": None,
                "joined_at": referred_company.created_at,
            }
        )
    joined_count = sum(invite["status"] == "joined" for invite in invite_items)
    return ReferralStatsResponse(
        people_invited=len(invite_items),
        joined=joined_count,
        pending=len(invite_items) - joined_count,
        credits_earned=current_user.referral_credits,
        invites=invite_items,
    )

@router.get(
    "/{company_id}",
    response_model=UserResponse,
    summary="Get company by ID (Super Admin or Self)"
)
def read_company_by_id(
    company_id: int,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Get company by ID. Available to Super Admin or the company themselves.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )
    if current_user.id != company.id and current_user.role != Role.SUPERADMIN and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user does not have enough privileges",
        )
    return company

@router.delete(
    "/{company_id}",
    response_model=UserDeleteResponse,
    summary="Delete company by ID (Super Admin only)"
)
def delete_company(
    company_id: int,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_superadmin),
) -> Any:
    """
    Delete a company by ID.

    The summary always said this was for admins, but the route only asked for a
    login, so any company could delete any other one. It now asks for a Super
    Admin, which is the only role that exists above a company.
    """
    target_company = db.query(Company).filter(Company.id == company_id).first()
    if not target_company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    if current_user.id == target_company.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account",
        )

    db.delete(target_company)
    db.commit()
    return {
        "message": f"Company '{target_company.email}' (ID: {company_id}) has been deleted successfully",
        "deleted_user_id": company_id,
    }
