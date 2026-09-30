from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.core.database import Base


class ReferralInvite(Base):
    __tablename__ = "referral_invites"
    __table_args__ = (
        UniqueConstraint("referrer_company_id", "invited_email", name="uq_referral_invite_email"),
    )

    id = Column(Integer, primary_key=True, index=True)
    referrer_company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    invited_email = Column(String(255), nullable=False)
    source = Column(String(20), nullable=False, default="email", server_default="email")
    sent_at = Column(DateTime(timezone=True), nullable=True)
    joined_company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    joined_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)