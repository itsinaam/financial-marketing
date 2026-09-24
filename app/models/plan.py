from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.sql import func
from app.core.database import Base


class SubscriptionPlan(Base):
    """A plan on the pricing page. Editable by a super admin, so it lives in the database."""

    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(30), unique=True, nullable=False, index=True)
    name = Column(String(60), nullable=False)
    description = Column(Text, nullable=True)

    monthly_price = Column(Float, nullable=False, default=0)
    yearly_price = Column(Float, nullable=False, default=0)
    features = Column(JSON, nullable=False, default=list)

    badge = Column(String(30), nullable=True)
    # null means unlimited
    posts_per_month = Column(Integer, nullable=True)
    businesses = Column(Integer, nullable=True)

    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    sort_order = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
