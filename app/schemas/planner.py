from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

PeriodLiteral = Literal["week", "month"]
ModeLiteral = Literal["auto_schedule", "manual_approval"]


class GeneratePlanRequest(BaseModel):
    period: PeriodLiteral = Field("week", description="Plan a week or a month of content")
    start_date: Optional[str] = Field(
        None,
        description="First day of the plan (YYYY-MM-DD). Defaults to the day after today.",
    )
    platforms: str = Field(
        "linkedin",
        description="Comma-separated target platforms (linkedin, instagram, facebook, x)",
    )
    count: Optional[int] = Field(
        None,
        ge=1,
        le=31,
        description="How many posts to plan. Defaults to one per day of the period.",
    )
    post_time: str = Field("09:00", description="Time of day each planned post goes out (HH:MM)")
    mode: ModeLiteral = Field(
        "manual_approval",
        description="auto_schedule approves the plan so it publishes on its slots; manual_approval sends it to the Approval Queue",
    )
    topic: Optional[str] = Field(None, description="Optional theme or campaign to build the plan around")
    company_description: Optional[str] = Field(None, description="What the business does, from the brand profile")
    brand_tone: Optional[str] = Field(None, description="Brand tone, e.g. Professional")
    target_audience: Optional[str] = Field(None, description="Who the content is aimed at")
    language: str = Field("English (US)", description="Output language")


class PlannerItem(BaseModel):
    id: str
    content_type: Literal["post"] = "post"
    title: Optional[str] = None
    headline: Optional[str] = None
    caption: Optional[str] = None
    hashtags: Optional[str] = None
    platform: str
    date: Optional[str] = None
    start_time: Optional[str] = None
    language: Optional[str] = None
    ai_safety_score: Optional[int] = None
    is_approved: bool
    is_posted: bool
    image_url: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class GeneratePlanResponse(BaseModel):
    status: str
    period: PeriodLiteral
    mode: ModeLiteral
    start_date: str
    end_date: str
    count: int
    items: List[PlannerItem]
    note: Optional[str] = None


class PlannerQueueResponse(BaseModel):
    items: List[PlannerItem]
    total: int
    awaiting_approval: int
    scheduled: int
    published: int
