from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.core.plans import FREE_PLAN_CODE, PLANS_BY_CODE
from app.api.credentials import resolve_company
from app.models.companies import Company
from app.models.payment import Payment
from app.models.plan import SubscriptionPlan
from app.schemas.subscriptions import (
    AdminPlanResponse,
    CurrentSubscriptionResponse,
    PlanResponse,
    PlansResponse,
    StartCheckoutRequest,
    StartCheckoutResponse,
    UpdatePlanRequest,
)
from app.services.stripe_service import StripeService

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

_PERIOD_DAYS = {"monthly": 30, "yearly": 365}


def _all_plans(db: Session, include_inactive: bool = False) -> List[SubscriptionPlan]:
    query = db.query(SubscriptionPlan)
    if not include_inactive:
        query = query.filter(SubscriptionPlan.is_active == True)  # noqa: E712
    return query.order_by(SubscriptionPlan.sort_order.asc(), SubscriptionPlan.id.asc()).all()


def _plan_by_code(db: Session, code: str) -> SubscriptionPlan | None:
    return db.query(SubscriptionPlan).filter(SubscriptionPlan.code == code).first()


def _to_plan_response(plan: SubscriptionPlan, current_code: str) -> PlanResponse:
    return PlanResponse(
        code=plan.code,
        name=plan.name,
        tagline=plan.description or "",
        badge=plan.badge,
        monthly_price=plan.monthly_price,
        yearly_price=plan.yearly_price,
        yearly_price_per_month=float(int(plan.yearly_price // 12)),
        posts_per_month=plan.posts_per_month,
        businesses=plan.businesses,
        features=list(plan.features or []),
        is_current=plan.code == current_code,
    )


def _plan_from_description(description: str | None) -> tuple[str, str | None]:
    """Work out the plan of a payment made before plan_code was recorded."""
    text = (description or "").lower()
    # Longest names first so "Top Tier" isn't missed by a shorter match.
    for code in ("top_tier", "plus", "pro", "free"):
        if PLANS_BY_CODE[code]["name"].lower() in text:
            return code, ("yearly" if "yearly" in text else "monthly")
    return FREE_PLAN_CODE, None


def _current_subscription(db: Session, company_id: int) -> CurrentSubscriptionResponse:
    payment = (
        db.query(Payment)
        .filter(Payment.user_id == company_id, Payment.status == "succeeded")
        .order_by(Payment.created_at.desc())
        .first()
    )
    free = _plan_by_code(db, FREE_PLAN_CODE)

    if payment is None:
        return CurrentSubscriptionResponse(
            plan_code=FREE_PLAN_CODE,
            plan_name=free.name if free else "Free",
            is_free=True,
            posts_per_month=free.posts_per_month if free else None,
            businesses=free.businesses if free else None,
        )

    parsed_code, parsed_period = _plan_from_description(payment.description)
    code = payment.plan_code or parsed_code
    period = payment.billing_period or parsed_period or "monthly"
    plan = _plan_by_code(db, code) or free

    started_at = payment.created_at
    expires_at = None
    if started_at:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        expires_at = started_at + timedelta(days=_PERIOD_DAYS.get(period, 30))

    return CurrentSubscriptionResponse(
        plan_code=plan.code if plan else code,
        plan_name=plan.name if plan else code,
        billing_period=period,
        is_free=(plan.code if plan else code) == FREE_PLAN_CODE,
        amount_paid=payment.amount,
        started_at=started_at,
        expires_at=expires_at,
        is_expired=bool(expires_at and expires_at < datetime.now(timezone.utc)),
        posts_per_month=plan.posts_per_month if plan else None,
        businesses=plan.businesses if plan else None,
    )


@router.get("/plans", response_model=PlansResponse, summary="All plans with prices, limits and the current one marked")
def list_plans(
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    current = _current_subscription(db, company.id)
    return PlansResponse(
        plans=[_to_plan_response(plan, current.plan_code) for plan in _all_plans(db)],
        current_plan_code=current.plan_code,
    )


@router.get("/current", response_model=CurrentSubscriptionResponse, summary="The company's current plan")
def get_current_subscription(
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    return _current_subscription(db, company.id)


@router.get(
    "/admin/plans",
    response_model=List[AdminPlanResponse],
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="Every plan including hidden ones, for the Super Admin plans screen",
)
def list_plans_for_admin(db: Session = Depends(deps.get_db)) -> Any:
    return [
        AdminPlanResponse(id=plan.id, is_active=plan.is_active, **_to_plan_response(plan, "").model_dump())
        for plan in _all_plans(db, include_inactive=True)
    ]


@router.put(
    "/admin/plans/{plan_id}",
    response_model=AdminPlanResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="Edit a plan's pricing, description and features (Super Admin only)",
)
def update_plan(
    plan_id: int,
    payload: UpdatePlanRequest,
    db: Session = Depends(deps.get_db),
) -> Any:
    """
    Backs the Edit plan dialog. Whatever is saved here is what customers see on the
    pricing page and what a checkout charges, so the price is never taken from the browser.
    """
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No plan with id {plan_id}.")

    data = payload.model_dump(exclude_unset=True)
    if "features" in data:
        cleaned = [f.strip() for f in (data["features"] or []) if f and f.strip()]
        if not cleaned:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Add at least one feature.")
        data["features"] = cleaned

    for field, value in data.items():
        setattr(plan, field, value.strip() if isinstance(value, str) else value)

    db.commit()
    db.refresh(plan)
    return AdminPlanResponse(id=plan.id, is_active=plan.is_active, **_to_plan_response(plan, "").model_dump())


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
    The price comes from the saved plan, never from the request, so a checkout can't
    be started for an amount the caller chose.
    """
    code = payload.plan_code.strip().lower()
    plan = _plan_by_code(db, code)
    if plan is None or not plan.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown plan '{payload.plan_code}'.",
        )
    if plan.code == FREE_PLAN_CODE or plan.monthly_price <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The Free plan doesn't need a payment.",
        )

    company: Company = resolve_company(db, auth, company_id)
    amount = plan.yearly_price if payload.billing_period == "yearly" else plan.monthly_price
    label = f"{plan.name} Plan ({'Yearly' if payload.billing_period == 'yearly' else 'Monthly'})"

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
            plan_code=plan.code,
            billing_period=payload.billing_period,
        )
    )
    db.commit()

    return StartCheckoutResponse(
        checkout_url=session["checkout_url"],
        session_id=session["session_id"],
        plan_code=plan.code,
        billing_period=payload.billing_period,
        amount=amount,
    )
