from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SupportMessageResponse(BaseModel):
    """One chat bubble. `sender_role` tells the UI which side to draw it on."""

    id: int
    company_id: int
    sender_id: Optional[int] = None
    sender_role: str
    sender_name: str
    body: str
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class SendSupportMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000, description="The message text")


class SupportThreadResponse(BaseModel):
    """A whole conversation, oldest message first."""

    company_id: int
    company_name: str
    company_email: str
    messages: List[SupportMessageResponse]
    unread_count: int = 0
    # Whether the far end of this thread has a live socket right now. Always
    # False on a host that cannot hold connections open.
    other_online: bool = False


class SupportConversationRow(BaseModel):
    """One row of the Super Admin's Support inbox."""

    company_id: int
    company_name: str
    company_email: str
    avatar_url: Optional[str] = None
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None
    last_sender_role: Optional[str] = None
    unread_count: int = 0


class SupportConversationsResponse(BaseModel):
    conversations: List[SupportConversationRow]
    total_unread: int = 0


class UnreadCountResponse(BaseModel):
    """Feeds the badge on the round Support button."""

    unread_count: int = 0


class MarkReadResponse(BaseModel):
    marked_read: int = 0
