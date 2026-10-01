from typing import Dict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core import deps
from app.models.companies import Company, Role
from app.models.referral import ReferralInvite
from app.schemas.referrals import (
    AdminReferralDashboardResponse,
    AdminReferralInviteItem,
    AdminReferrerStats,
)
from app.services.referral_service import REFERRAL_REWARD_CREDITS

router = APIRouter()


@router.get(
    "/admin",
    response_model=AdminReferralDashboardResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="Referral totals and invite details for the Super Admin",
)
def get_admin_referrals(db: Session = Depends(deps.get_db)) -> AdminReferralDashboardResponse:
    companies = db.query(Company).filter(Company.role == Role.COMPANY).all()
    companies_by_id = {company.id: company for company in companies}
    referral_rows: Dict[int, Dict[str, dict]] = {company.id: {} for company in companies}

    invites = (
        db.query(ReferralInvite)
        .order_by(ReferralInvite.created_at.desc(), ReferralInvite.id.desc())
        .all()
    )
    for invite in invites:
        if invite.referrer_company_id not in companies_by_id:
            continue
        invited_email = invite.invited_email.strip().lower()
        referral_rows[invite.referrer_company_id][invited_email] = {
            "invited_email": invited_email,
            "status": "joined" if invite.joined_company_id is not None else "pending",
            "source": invite.source if invite.source in {"email", "link"} else "link",
            "credits": REFERRAL_REWARD_CREDITS if invite.joined_company_id is not None else 0,
            "sent_at": invite.sent_at,
            "joined_at": invite.joined_at,
            "created_at": invite.created_at,
        }

    referred_companies = (
        db.query(Company)
        .filter(
            Company.role == Role.COMPANY,
            Company.referred_by_company_id.isnot(None),
        )
        .all()
    )
    for referred_company in referred_companies:
        referrer_id = referred_company.referred_by_company_id
        if referrer_id not in referral_rows:
            continue
        invited_email = referred_company.email.strip().lower()
        row = referral_rows[referrer_id].get(invited_email)
        if row is None:
            referral_rows[referrer_id][invited_email] = {
                "invited_email": invited_email,
                "status": "joined",
                "source": "link",
                "credits": REFERRAL_REWARD_CREDITS,
                "sent_at": None,
                "joined_at": referred_company.created_at,
                "created_at": referred_company.created_at,
            }
        elif row["status"] != "joined":
            row.update(
                status="joined",
                credits=REFERRAL_REWARD_CREDITS,
                joined_at=referred_company.created_at,
            )

    referrer_stats = []
    for referrer in companies:
        rows = referral_rows[referrer.id]
        if not rows:
            continue
        referrals = [
            AdminReferralInviteItem(**row)
            for row in sorted(
                rows.values(),
                key=lambda item: str(
                    item["created_at"] or item["joined_at"] or item["sent_at"] or ""
                ),
                reverse=True,
            )
        ]
        joined_count = sum(referral.status == "joined" for referral in referrals)
        referrer_stats.append(
            AdminReferrerStats(
                company_id=referrer.id,
                company_name=referrer.name,
                company_email=referrer.email,
                total_invites=len(referrals),
                joined=joined_count,
                pending=len(referrals) - joined_count,
                credits_earned=referrer.referral_credits or 0,
                referrals=referrals,
            )
        )

    referrer_stats.sort(key=lambda item: item.total_invites, reverse=True)
    return AdminReferralDashboardResponse(
        total_invites=sum(referrer.total_invites for referrer in referrer_stats),
        joined=sum(referrer.joined for referrer in referrer_stats),
        active_referrers=len(referrer_stats),
        credits_awarded=sum(company.referral_credits or 0 for company in companies),
        referrers=referrer_stats,
    )