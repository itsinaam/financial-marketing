from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

ContentTypeLiteral = Literal["post", "blog"]


class CalendarItem(BaseModel):
    id: str
    content_type: ContentTypeLiteral
    title: Optional[str] = None
    preview: Optional[str] = None
    image_url: Optional[str] = None
    platform: str
    date: Optional[str] = None
    start_time: Optional[str] = None
    scheduled_at: Optional[datetime] = Field(None, description="Scheduled date and time combined, in UTC")
    is_posted: bool
    posted_at: Optional[datetime] = None
    post_error: Optional[str] = None
    published_url: Optional[str] = None
    language: Optional[str] = None
    ai_safety_score: Optional[int] = None


class CalendarResponse(BaseModel):
    items: List[CalendarItem]
    total: int
    scheduled: int
    published: int
    unscheduled: int = Field(0, description="Approved drafts with no date yet, so they cannot be placed on the calendar")


class RescheduleRequest(BaseModel):
    date: str = Field(..., description="New date (YYYY-MM-DD)")
    start_time: str = Field(..., description="New time (HH:MM)")
