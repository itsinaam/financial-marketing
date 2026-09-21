from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

ContentTypeLiteral = Literal["post", "blog"]


class DashboardStats(BaseModel):
    drafted_this_week: int = Field(..., description="Posts and blogs created since Monday")
    drafted_platforms: int = Field(..., description="How many different platforms those drafts target")
    awaiting_review: int = Field(..., description="Unapproved, unpublished drafts")
    oldest_awaiting_at: Optional[datetime] = Field(None, description="When the oldest draft awaiting review was created")
    scheduled: int = Field(..., description="Approved items with a future slot, not yet published")
    next_scheduled_at: Optional[datetime] = Field(None, description="The soonest upcoming slot, in UTC")
    published_this_month: int
    published_platforms: List[str] = Field(default_factory=list, description="Platforms published to this month")


class PlatformSummary(BaseModel):
    platform: str
    drafted: int = Field(..., description="Unapproved and unpublished")
    scheduled: int = Field(..., description="Approved but not yet published")
    published: int


class TopPost(BaseModel):
    id: str
    content_type: ContentTypeLiteral
    title: Optional[str] = None
    text: Optional[str] = None
    platform: str
    image_url: Optional[str] = None
    posted_at: Optional[datetime] = None
    metrics_available: bool = Field(
        False,
        description="Engagement is not collected from the platforms yet, so views, reactions and reposts are null",
    )
    views: Optional[int] = None
    reactions: Optional[int] = None
    reposts: Optional[int] = None


class ActivityItem(BaseModel):
    type: Literal["drafted", "published", "publish_failed", "library_added"]
    message: str
    content_type: Optional[str] = None
    item_id: Optional[str] = None
    platform: Optional[str] = None
    at: datetime


class ComingUpItem(BaseModel):
    id: str
    content_type: ContentTypeLiteral
    title: Optional[str] = None
    platform: str
    scheduled_at: datetime


class DashboardResponse(BaseModel):
    stats: DashboardStats
    platforms: List[PlatformSummary]
    top_post: Optional[TopPost] = None
    recent_activity: List[ActivityItem]
    coming_up: List[ComingUpItem]
