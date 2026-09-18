from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

ContentTypeLiteral = Literal["post", "blog"]


class ApprovalQueueItem(BaseModel):
    id: str
    content_type: ContentTypeLiteral
    title: Optional[str] = None
    preview: Optional[str] = None
    image_url: Optional[str] = None
    platform: str
    ai_safety_score: Optional[int] = None
    language: Optional[str] = None
    is_flagged: bool
    date: Optional[str] = None
    start_time: Optional[str] = None
    created_at: Optional[datetime] = None


class ApprovalQueueResponse(BaseModel):
    items: List[ApprovalQueueItem]
    total: int
    ready_for_review: int
    flagged: int


class ApprovalDecisionRequest(BaseModel):
    post_ids: List[str] = Field(default_factory=list, description="Generated post IDs to act on")
    blog_ids: List[str] = Field(default_factory=list, description="Generated blog IDs to act on")
    is_approved: bool = Field(default=True, description="True to approve, false to send back to drafts")


class ApprovalDecisionResponse(BaseModel):
    status: str
    message: str
    updated_count: int
    updated_post_ids: List[str]
    updated_blog_ids: List[str]
    missing_ids: List[str]
