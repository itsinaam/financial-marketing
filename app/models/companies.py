import enum
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.core.database import Base

class Role(str, enum.Enum):
    SUPERADMIN = "superadmin"
    COMPANY = "company"

class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    avatar_url = Column(String(500), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(Enum(Role), default=Role.COMPANY, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_superuser = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    payments = relationship("Payment", back_populates="company", cascade="all, delete-orphan", lazy="selectin")
    credentials = relationship("Credentials", back_populates="company", cascade="all, delete-orphan", lazy="selectin")


    @property
    def plan(self) -> dict | None:
        # Only a paid payment counts as an active plan. A checkout session that was
        # opened but never paid stays "pending" and must not unlock the plan.
        succeeded = [p for p in self.payments if p.status == "succeeded"]
        if not succeeded:
            return None

        chosen = sorted(
            succeeded,
            key=lambda p: (p.created_at.timestamp() if p.created_at else 0, p.id or 0),
        )[-1]

        desc = chosen.description or ""
        prefix = "Checkout Session for "
        if desc.startswith(prefix):
            plan_name = desc[len(prefix):].strip()
        else:
            plan_name = desc or "Standard Plan"

        return {
            "id": chosen.id,
            "plan_name": plan_name,
            "product_name": plan_name,
            "amount": chosen.amount,
            "currency": chosen.currency,
            "status": chosen.status,
            "description": chosen.description,
            "stripe_checkout_session_id": chosen.stripe_checkout_session_id,
            "stripe_payment_intent_id": chosen.stripe_payment_intent_id,
            "created_at": chosen.created_at,
        }

