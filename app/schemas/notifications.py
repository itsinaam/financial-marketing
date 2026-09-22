from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

ProviderLiteral = Literal["whatsapp", "slack", "teams"]


class NotificationTriggers(BaseModel):
    ready_for_approval: bool = Field(..., description="New Post Ready for Approval")
    published: bool = Field(..., description="Post Published Successfully")
    failed: bool = Field(..., description="Post Failed / Error Alerts")


class NotificationChannelResponse(BaseModel):
    provider: ProviderLiteral
    is_connected: bool
    target: Optional[str] = Field(None, description="Channel name, or the WhatsApp group invite link")
    webhook_configured: bool = Field(..., description="Whether a webhook is saved; the URL itself is never returned")
    can_send: bool = Field(..., description="False for WhatsApp, which can't be messaged through an invite link")
    triggers: NotificationTriggers
    last_error: Optional[str] = None
    updated_at: Optional[datetime] = None


class NotificationChannelList(BaseModel):
    channels: List[NotificationChannelResponse]


class SaveChannelRequest(BaseModel):
    target: Optional[str] = Field(
        None,
        max_length=500,
        description="Slack/Teams: the channel name to show. WhatsApp: the group invite link.",
    )
    webhook_url: Optional[str] = Field(
        None,
        description="Slack/Teams only: the incoming webhook URL. Leave out to keep the saved one.",
    )
    ready_for_approval: Optional[bool] = None
    published: Optional[bool] = None
    failed: Optional[bool] = None


class TestChannelResponse(BaseModel):
    success: bool
    message: str
