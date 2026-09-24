from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.core.plans import (
    FREE_PLAN_CODE,
    PLANS,
    PLANS_BY_CODE,
    plan_label,
    price_for,
    yearly_price,
)
from app.api.credentials import resolve_company
from app.models.payment import Payment
from app.schemas.subscriptions import (
    CurrentSubscriptionResponse,
    PlanResponse,
    PlansResponse,
    StartCheckoutRequest,
    StartCheckoutResponse,
)
from app.services.stripe_service import StripeService

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

_PERIOD_DAYS = {"monthly": 30, "yearly": 365}


def _plan_from_description(description: str | None) -> tuple[str, str | None]:
    """Work out the plan of a payment made before plan_code was recorded."""
    text = (description or "").lower()
    # Longest names first so "Top Tier" isn't missed by a shorter match.
    for code in ("top_tier", "plus", "pro", "free"):
        if PLANS_BY_CODE[code]["name"].lower() in text:
            period = "yearly" if "yearly" in text else "monthly"
            return code, period
    return FREE_PLAN_CODE, None


def _latest_paid_payment(db: Session, company_id: int) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.user_id == company_id, Payment.status == "succeeded")
        .order_by(Payment.created_at.desc())
        .first()
    )


def _current_subscription(db: Session, company_id: int) -> CurrentSubscriptionResponse:
    payment = _latest_paid_payment(db, company_id)
    if payment is None:
        free = PLANS_BY_CODE[FREE_PLAN_CODE]
        return CurrentSubscriptionResponse(
            plan_code=FREE_PLAN_CODE,
            plan_name=free["name"],
            is_free=True,
            posts_per_month=free["posts_per_month"],
            businesses=free["businesses"],
        )

    code = payment.plan_code or _plan_from_description(payment.description)[0]
    period = payment.billing_period or _plan_from_description(payment.description)[1] or "monthly"
    plan = PLANS_BY_CODE.get(code, PLANS_BY_CODE[FREE_PLAN_CODE])

    started_at = payment.created_at
    expires_at = None
    if started_at:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        expires_at = started_at + timedelta(days=_PERIOD_DAYS.get(period, 30))

    return CurrentSubscriptionResponse(
        plan_code=plan["code"],
        plan_name=plan["name"],
        billing_period=period,
        is_free=plan["code"] == FREE_PLAN_CODE,
        amount_paid=payment.amount,
        started_at=started_at,
        expires_at=expires_at,
        is_expired=bool(expires_at and expires_at < datetime.now(timezone.utc)),
        posts_per_month=plan["posts_per_month"],
        businesses=plan["businesses"],
    )


@router.get("/plans", response_model=PlansResponse, summary="All plans with prices, limits and the current one marked")
def list_plans(
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    current = _current_subscription(db, company.id)

    plans = []
    for plan in PLANS:
        yearly = yearly_price(plan["monthly_price"])
        plans.append(
            PlanResponse(
                code=plan["code"],
                name=plan["name"],
                tagline=plan["tagline"],
                badge=plan["badge"],
                monthly_price=float(plan["monthly_price"]),
                yearly_price=float(yearly),
                yearly_price_per_month=float(yearly // 12),
                posts_per_month=plan["posts_per_month"],
                businesses=plan["businesses"],
                features=plan["features"],
                is_current=plan["code"] == current.plan_code,
            )
        )

    return PlansResponse(plans=plans, current_plan_code=current.plan_code)


@router.get("/current", response_model=CurrentSubscriptionResponse, summary="The company's current plan")
def get_current_subscription(
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    return _current_subscription(db, company.id)


@router.post(
    "/checkout",
    response_model=StartCheckoutResponse,
    summary="Start Stripe checkout for a plan (the Get Started button)",
)
def start_checkout(
    payload: StartCheckoutRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    The price comes from the plan on the server, never from the request, so a
    checkout can't be started for an amount the caller chose.
    """
    code = payload.plan_code.strip().lower()
    if code not in PLANS_BY_CODE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown plan '{payload.plan_code}'. Use one of: {', '.join(PLANS_BY_CODE)}.",
        )
    if code == FREE_PLAN_CODE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The Free plan doesn't need a payment.",
        )

    company = resolve_company(db, auth, company_id)
    amount = price_for(code, payload.billing_period)
    label = plan_label(code, payload.billing_period)

    session = StripeService.create_checkout_session(
        user_email=company.email,
        amount=amount,
        currency="usd",
        product_name=label,
        success_url=payload.success_url,
        cancel_url=payload.cancel_url,
    )

    db.add(
        Payment(
            user_id=company.id,
            stripe_checkout_session_id=session["session_id"],
            amount=amount,
            currency="usd",
            status="pending",
            description=f"Checkout Session for {label}",
            plan_code=code,
            billing_period=payload.billing_period,
        )
    )
    db.commit()

    return StartCheckoutResponse(
        checkout_url=session["checkout_url"],
        session_id=session["session_id"],
        plan_code=code,
        billing_period=payload.billing_period,
        amount=amount,
    )
