from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class SaveCredentialsRequest(BaseModel):
    client_id: str = Field(..., description="Platform Client ID / App ID")
    client_secret: str = Field(..., description="Platform Client Secret")
    platform: str = Field(default="linkedin", description="Platform name (e.g. linkedin)")
    company_id: Optional[int] = Field(None, description="Optional Company ID if not authenticated via Bearer token")


class PostResponse(BaseModel):
    success: bool
    platform: str
    post_id: Optional[str] = None
    target: Optional[str] = None
    message: str
    image_attached: bool = False
    authorization_url: Optional[str] = None


class CredentialsResponse(BaseModel):
    id: int
    company_id: int
    platform: str
    client_id: str
    access_token: Optional[str] = None
    organization_id: Optional[str] = None
    message: Optional[str] = None
    authorization_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PlatformStatusResponse(BaseModel):
    platform: str = Field(..., description="Platform name (e.g. linkedin, instagram, facebook, x)")
    is_connected: bool = Field(..., description="Whether the account is connected (true/false)")


class OAuthConnectResponse(BaseModel):
    company_id: int
    platform: str
    authorization_url: str
    redirect_uri: str
    message: str




