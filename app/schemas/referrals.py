from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class ReferralEmailRequest(BaseModel):
    email: EmailStr


class ReferralLinkResponse(BaseModel):
    referral_link: str


class ReferralEmailResponse(ReferralLinkResponse):
    message: str = Field(default="Referral email sent successfully.")


class ReferralInviteItem(BaseModel):
    invited_email: EmailStr
    status: Literal["pending", "joined"]
    source: Literal["email", "link"]
    credits: int
    sent_at: Optional[datetime] = None
    joined_at: Optional[datetime] = None


class ReferralStatsResponse(BaseModel):
    people_invited: int
    joined: int
    pending: int
    credits_earned: int
    invites: List[ReferralInviteItem]


class AdminReferralInviteItem(BaseModel):
    invited_email: EmailStr
    status: Literal["pending", "joined"]
    source: Literal["email", "link"]
    credits: int
    sent_at: Optional[datetime] = None
    joined_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class AdminReferrerStats(BaseModel):
    company_id: int
    company_name: str
    company_email: EmailStr
    total_invites: int
    joined: int
    pending: int
    credits_earned: int
    referrals: List[AdminReferralInviteItem]


class AdminReferralDashboardResponse(BaseModel):
    total_invites: int
    joined: int
    active_referrers: int
    credits_awarded: int
    referrers: List[AdminReferrerStats]
