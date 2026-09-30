from pydantic import BaseModel, EmailStr, Field


class ReferralEmailRequest(BaseModel):
    email: EmailStr


class ReferralLinkResponse(BaseModel):
    referral_link: str


class ReferralEmailResponse(ReferralLinkResponse):
    message: str = Field(default="Referral email sent successfully.")
