from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

class CreateCheckoutSession(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in currency units, e.g. 10.00 for $10 USD")
    currency: str = Field(default="usd", max_length=10)
    product_name: str = Field(default="Financial Service Payment", max_length=255)
    success_url: str = Field(default="http://localhost:8000/docs#/Payments/get_my_payments_api_v1_payments_history_get")
    cancel_url: str = Field(default="http://localhost:8000/docs")

class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id: str

class CreatePaymentIntent(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in currency units, e.g. 15.50")
    currency: str = Field(default="usd", max_length=10)
    description: str | None = Field(default=None, max_length=500)

class PaymentIntentResponse(BaseModel):
    client_secret: str
    payment_intent_id: str
    amount: float
    currency: str
    status: str

class PaymentResponse(BaseModel):
    id: int
    user_id: int
    amount: float
    currency: str
    status: str
    stripe_payment_intent_id: str | None = None
    stripe_checkout_session_id: str | None = None
    description: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
