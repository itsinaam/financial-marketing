from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func
from app.core.database import Base


class NotificationChannel(Base):
    """Where a company's team gets alerted (Slack, Teams, WhatsApp) and for which events."""

    __tablename__ = "notification_channels"
    __table_args__ = (UniqueConstraint("company_id", "provider", name="uq_notification_channel_company_provider"),)

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    provider = Column(String(20), nullable=False)

    # What the screen shows: a channel name, or a WhatsApp group invite link.
    target = Column(String(500), nullable=True)
    # Where messages are actually delivered; a secret, never returned in full.
    webhook_url = Column(Text, nullable=True)

    notify_ready_for_approval = Column(Boolean, default=True, nullable=False, server_default="true")
    notify_published = Column(Boolean, default=False, nullable=False, server_default="false")
    notify_failed = Column(Boolean, default=True, nullable=False, server_default="true")

    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
