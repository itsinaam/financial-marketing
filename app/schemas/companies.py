from typing import List
from datetime import datetime
from pydantic import BaseModel, EmailStr, ConfigDict, model_validator
from app.models.companies import Role
from app.schemas.payment import PaymentResponse

class PlanDetails(BaseModel):
    id: int | None = None
    plan_code: str
    plan_name: str
    product_name: str
    amount: float
    currency: str
    status: str
    description: str | None = None
    stripe_checkout_session_id: str | None = None
    stripe_payment_intent_id: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)

class UserDeleteResponse(BaseModel):
    message: str
    deleted_user_id: int

class UserBase(BaseModel):
    email: EmailStr
    name: str | None = None
    role: Role = Role.COMPANY
    is_active: bool = True

class UserCreate(UserBase):
    password: str

class UserUpdate(BaseModel):
    email: EmailStr | None = None
    password: str | None = None
    name: str | None = None
    full_name: str | None = None
    role: Role | None = None
    is_active: bool | None = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class SignupRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    confirm_password: str

    @model_validator(mode="after")
    def passwords_match(self) -> "SignupRequest":
        if self.password != self.confirm_password:
            raise ValueError("Password and confirm password do not match")
        return self

class UserResponse(UserBase):
    id: int
    is_superuser: bool
    created_at: datetime | None = None
    avatar_url: str | None = None
    plan: PlanDetails
    payments: List[PaymentResponse] = []

    model_config = ConfigDict(from_attributes=True)

