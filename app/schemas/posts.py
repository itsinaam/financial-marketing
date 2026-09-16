from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict


class GeneratedPostResponse(BaseModel):
    id: str
    company_id: int
    prompt: str
    platform: str
    title: Optional[str] = None
    headline: Optional[str] = None
    caption: str
    hashtags: Optional[str] = None
    image_url: Optional[str] = None
    reference_image_id: Optional[str] = None
    reference_image_url: Optional[str] = None
    tone: Optional[str] = None
    language: Optional[str] = None
    ai_safety_score: Optional[int] = None
    date: Optional[str] = None
    start_time: Optional[str] = None
    is_approved: bool
    is_posted: bool
    posted_at: Optional[datetime] = None
    post_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class GeneratePostResult(BaseModel):
    status: str
    count: int
    posts: List[GeneratedPostResponse]
    note: Optional[str] = None


class EditPostRequest(BaseModel):
    caption: Optional[str] = None
    hashtags: Optional[str] = None
    title: Optional[str] = None
    headline: Optional[str] = None
    tone: Optional[str] = None
    language: Optional[str] = None
    date: Optional[str] = None
    start_time: Optional[str] = None


class ApprovePostsRequest(BaseModel):
    post_ids: List[str]
    is_approved: bool = True


class ApprovePostsResponse(BaseModel):
    status: str
    message: str
    approved_count: int
    approved_ids: List[str]
    missing_ids: List[str]


class PublishPostResponse(BaseModel):
    status: str
    message: str
    post_id: str
    details: dict
