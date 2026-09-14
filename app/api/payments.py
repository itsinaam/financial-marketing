from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, Request, Header, status
from sqlalchemy.orm import Session

from app.core import deps
from app.models.payment import Payment
from app.models.companies import Company
from app.schemas.payment import (
    CreateCheckoutSession,
    CheckoutSessionResponse,
    PaymentResponse,
)
from app.services.stripe_service import StripeService

router = APIRouter()

@router.post("/create-checkout-session", response_model=CheckoutSessionResponse, summary="Create Stripe Checkout Session link")
def create_checkout_session(
    checkout_in: CreateCheckoutSession,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Generate a hosted Stripe Checkout URL for card payments.
    """
    session_data = StripeService.create_checkout_session(
        user_email=current_user.email,
        amount=checkout_in.amount,
        currency=checkout_in.currency,
        product_name=checkout_in.product_name,
        success_url=checkout_in.success_url,
        cancel_url=checkout_in.cancel_url,
    )
    
    # Store pending payment record in DB
    payment = Payment(
        user_id=current_user.id,
        stripe_checkout_session_id=session_data["session_id"],
        amount=checkout_in.amount,
        currency=checkout_in.currency.lower(),
        status="pending",
        description=f"Checkout Session for {checkout_in.product_name}",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    
    return session_data

@router.get("/history",response_model=List[PaymentResponse], summary="Get user payment transaction history")
def get_my_payments(
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    Retrieve payment history for current logged in user.
    """
    payments = (
        db.query(Payment)
        .filter(Payment.user_id == current_user.id)
        .order_by(Payment.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return payments

@router.post("/webhook", summary="Stripe Webhook Event Listener")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
    db: Session = Depends(deps.get_db),
) -> Any:
    """
    Real-time Stripe Webhook listener for payment events.
    """
    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing stripe-signature header",
        )
        
    payload = await request.body()
    event = StripeService.construct_webhook_event(payload, stripe_signature)
    
    event_type = event["type"]
    event_data = event["data"]["object"]
    
    if event_type == "checkout.session.completed":
        session_id = event_data.get("id")
        payment = db.query(Payment).filter(Payment.stripe_checkout_session_id == session_id).first()
        if payment:
            payment.status = "succeeded"
            db.commit()
            
    elif event_type == "payment_intent.succeeded":
        intent_id = event_data.get("id")
        payment = db.query(Payment).filter(Payment.stripe_payment_intent_id == intent_id).first()
        if payment:
            payment.status = "succeeded"
            db.commit()
            
    elif event_type == "payment_intent.payment_failed":
        intent_id = event_data.get("id")
        payment = db.query(Payment).filter(Payment.stripe_payment_intent_id == intent_id).first()
        if payment:
            payment.status = "failed"
            db.commit()
            
    return {"status": "success"}
