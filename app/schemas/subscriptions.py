from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

BillingPeriodLiteral = Literal["monthly", "yearly"]


class PlanResponse(BaseModel):
    code: str
    name: str
    tagline: str
    badge: Optional[str] = Field(None, description="e.g. MOST POPULAR or PREMIUM")
    monthly_price: float
    yearly_price: float = Field(..., description="A year up front, 20% off the monthly rate")
    yearly_price_per_month: float = Field(..., description="The 'Billed $X monthly' figure on the yearly tab")
    currency: str = "usd"
    posts_per_month: Optional[int] = Field(None, description="null means unlimited")
    businesses: Optional[int] = Field(None, description="null means no limit")
    features: List[str]
    is_current: bool = False


class PlansResponse(BaseModel):
    plans: List[PlanResponse]
    current_plan_code: str


class CurrentSubscriptionResponse(BaseModel):
    plan_code: str
    plan_name: str
    billing_period: Optional[BillingPeriodLiteral] = None
    is_free: bool
    amount_paid: Optional[float] = None
    started_at: Optional[datetime] = Field(None, description="When the paid plan was confirmed")
    expires_at: Optional[datetime] = Field(None, description="When the paid period runs out")
    is_expired: bool = False
    posts_per_month: Optional[int] = None
    businesses: Optional[int] = None


class StartCheckoutRequest(BaseModel):
    plan_code: str = Field(..., description="pro, plus or top_tier")
    billing_period: BillingPeriodLiteral = "monthly"
    success_url: str = Field(..., description="Where Stripe returns after a successful payment")
    cancel_url: str = Field(..., description="Where Stripe returns if the customer backs out")


class StartCheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str
    plan_code: str
    billing_period: BillingPeriodLiteral
    amount: float
