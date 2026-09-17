import stripe
from fastapi import HTTPException, status
from app.core.config import settings

# Initialize Stripe API key
stripe.api_key = settings.STRIPE_SECRET_KEY

class StripeService:
    @staticmethod
    def create_checkout_session(
        user_email: str,
        amount: float,
        currency: str = "usd",
        product_name: str = "Financial Service Payment",
        success_url: str = "http://localhost:8000/docs",
        cancel_url: str = "http://localhost:8000/docs",
    ) -> dict:
        """
        Creates a Stripe Checkout Session for hosted payment page.
        """
        try:
            # Stripe amounts are in cents/smallest currency unit
            amount_in_cents = int(amount * 100)
            
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                customer_email=user_email,
                line_items=[
                    {
                        "price_data": {
                            "currency": currency.lower(),
                            "product_data": {
                                "name": product_name,
                            },
                            "unit_amount": amount_in_cents,
                        },
                        "quantity": 1,
                    }
                ],
                mode="payment",
                success_url=success_url,
                cancel_url=cancel_url,
            )
            return {
                "checkout_url": session.url,
                "session_id": session.id,
            }
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Stripe Checkout Error: {str(e)}"
            )

    @staticmethod
    def create_payment_intent(
        amount: float,
        currency: str = "usd",
        description: str | None = None
    ) -> dict:
        """
        Creates a Stripe PaymentIntent for custom inline card processing.
        """
        try:
            amount_in_cents = int(amount * 100)
            intent = stripe.PaymentIntent.create(
                amount=amount_in_cents,
                currency=currency.lower(),
                description=description,
                automatic_payment_methods={"enabled": True},
            )
            return {
                "client_secret": intent.client_secret,
                "payment_intent_id": intent.id,
                "amount": amount,
                "currency": currency,
                "status": intent.status,
            }
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Stripe PaymentIntent Error: {str(e)}"
            )

    @staticmethod
    def retrieve_checkout_session(session_id: str) -> dict:
        """
        Fetch a Checkout Session from Stripe to confirm whether it was actually paid.
        """
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            return {
                "payment_status": getattr(session, "payment_status", None),
                "status": getattr(session, "status", None),
                "payment_intent": getattr(session, "payment_intent", None),
            }
        except stripe.error.StripeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Stripe Session Retrieve Error: {str(e)}"
            )

    @staticmethod
    def construct_webhook_event(payload: bytes, sig_header: str) -> stripe.Event:
        """
        Constructs and verifies a Stripe Webhook Event signature.
        """
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
            return event
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid payload"
            )
        except stripe.error.SignatureVerificationError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Stripe signature verification"
            )
