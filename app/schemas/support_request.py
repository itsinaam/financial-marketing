from datetime import datetime
from typing import List, Optional

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SubmitSupportRequest(BaseModel):
    """The three fields the Support form asks for."""

    name: str = Field(min_length=1, max_length=255, description="Full name")
    email: EmailStr = Field(description="Where the reply should go")
    message: str = Field(min_length=1, max_length=5000, description="What they need help with")


class SupportRequestResponse(BaseModel):
    id: int
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    name: str
    email: str
    message: str
    email_sent: bool = False
    status: str = "open"
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class UpdateSupportRequestStatus(BaseModel):
    status: Literal["open", "closed"]


class UpdateSupportRequestsStatus(BaseModel):
    """Move several requests to the same status in one go."""

    ids: List[int] = Field(min_length=1, description="The requests to update")
    status: Literal["open", "closed"]


class UpdateStatusResponse(BaseModel):
    updated: int = 0


class DeleteSupportRequests(BaseModel):
    """Ids to remove in one go."""

    ids: List[int] = Field(min_length=1, description="The requests to delete")


class DeleteSupportRequestsResponse(BaseModel):
    deleted: int = 0


class SupportRequestEventResponse(BaseModel):
    """One line of the timeline on the detail page."""

    id: int
    kind: str
    detail: Optional[str] = None
    actor: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class SupportRequestDetailResponse(SupportRequestResponse):
    """The request plus everything that has happened to it, oldest first."""

    events: List[SupportRequestEventResponse] = []


class SupportRequestsResponse(BaseModel):
    """Everything the Super Admin's table needs, newest first."""

    requests: List[SupportRequestResponse]
    total: int = 0
