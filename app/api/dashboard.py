import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.blog import GeneratedBlog
from app.models.library import Library
from app.models.post import GeneratedPost
from app.schemas.dashboard import (
    ActivityItem,
    ComingUpItem,
    DashboardResponse,
    DashboardStats,
    PlatformSummary,
    TopPost,
)
from app.services.scheduler_service import LOCAL_TZ, parse_scheduled_datetime

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

# Always shown as cards, even with nothing on them yet, so the layout stays stable.
CORE_PLATFORMS = ["linkedin", "instagram", "facebook", "x"]
ACTIVITY_LIMIT = 10
COMING_UP_LIMIT = 5

_TAG_RE = re.compile(r"<[^>]+>")


def _plain(raw: Optional[str], limit: int = 280) -> Optional[str]:
    if not raw:
        return None
    text = re.sub(r"\s{2,}", " ", _TAG_RE.sub(" ", raw)).strip()
    return text[:limit] if text else None


def _title(record: Any) -> str:
    return record.title or getattr(record, "headline", None) or "Untitled"


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("", response_model=DashboardResponse, summary="Everything the dashboard shows, in one call")
def get_dashboard(
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Stat cards, per-platform counts, the latest published post, recent activity and
    the next few scheduled items. "This week" starts on Monday and "this month" on the
    1st, both in the app's local timezone, to match the Calendar.
    """
    company = resolve_company(db, auth, company_id)

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    week_start = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    posts = db.query(GeneratedPost).filter(GeneratedPost.company_id == company.id).all()
    blogs = db.query(GeneratedBlog).filter(GeneratedBlog.company_id == company.id).all()
    records = [("post", p) for p in posts] + [("blog", b) for b in blogs]

    platforms = {name: {"drafted": 0, "scheduled": 0, "published": 0} for name in CORE_PLATFORMS}
    drafted_this_week = 0
    drafted_platforms = set()
    awaiting_created: List[datetime] = []
    upcoming: List[ComingUpItem] = []
    published_this_month = 0
    published_platforms = set()
    latest_published_this_week = None
    activity: List[ActivityItem] = []

    for kind, record in records:
        platform = (record.platform or "").lower()
        bucket = platforms.setdefault(platform, {"drafted": 0, "scheduled": 0, "published": 0})
        created_at = _as_utc(record.created_at)
        posted_at = _as_utc(record.posted_at)
        title = _title(record)

        if created_at and created_at >= week_start:
            drafted_this_week += 1
            drafted_platforms.add(platform)

        if record.is_posted:
            bucket["published"] += 1
            if posted_at and posted_at >= month_start:
                published_this_month += 1
                published_platforms.add(platform)
            if posted_at and posted_at >= week_start:
                if latest_published_this_week is None or posted_at > latest_published_this_week[2]:
                    latest_published_this_week = (kind, record, posted_at)
        elif record.is_approved:
            bucket["scheduled"] += 1
            scheduled_at = parse_scheduled_datetime(record.date, record.start_time)
            if scheduled_at and scheduled_at >= now_utc:
                upcoming.append(
                    ComingUpItem(
                        id=record.id,
                        content_type=kind,
                        title=title,
                        platform=platform,
                        scheduled_at=scheduled_at,
                    )
                )
        else:
            bucket["drafted"] += 1
            if created_at:
                awaiting_created.append(created_at)

        if created_at:
            activity.append(
                ActivityItem(
                    type="drafted",
                    message=f"Drafted {kind} for {platform}: {title}",
                    content_type=kind,
                    item_id=record.id,
                    platform=platform,
                    at=created_at,
                )
            )
        if record.is_posted and posted_at:
            activity.append(
                ActivityItem(
                    type="published",
                    message=f"Published to {platform}: {title}",
                    content_type=kind,
                    item_id=record.id,
                    platform=platform,
                    at=posted_at,
                )
            )
        elif record.post_error:
            activity.append(
                ActivityItem(
                    type="publish_failed",
                    message=f"Couldn't publish to {platform}: {record.post_error}",
                    content_type=kind,
                    item_id=record.id,
                    platform=platform,
                    at=_as_utc(record.updated_at) or created_at or now_utc,
                )
            )

    library_items = db.query(Library).filter(Library.company_id == company.id).all()
    for asset in library_items:
        added_at = _as_utc(asset.created_at)
        if added_at:
            activity.append(
                ActivityItem(
                    type="library_added",
                    message=f"Added to the library: {asset.name}",
                    item_id=asset.id,
                    at=added_at,
                )
            )

    upcoming.sort(key=lambda item: item.scheduled_at)
    activity.sort(key=lambda item: item.at, reverse=True)

    top_post = None
    if latest_published_this_week:
        kind, record, posted_at = latest_published_this_week
        top_post = TopPost(
            id=record.id,
            content_type=kind,
            title=_title(record),
            text=_plain(getattr(record, "caption", None) or getattr(record, "content", None)),
            platform=(record.platform or "").lower(),
            image_url=record.image_url,
            posted_at=posted_at,
        )

    stats = DashboardStats(
        drafted_this_week=drafted_this_week,
        drafted_platforms=len(drafted_platforms),
        awaiting_review=len(awaiting_created),
        oldest_awaiting_at=min(awaiting_created) if awaiting_created else None,
        scheduled=sum(bucket["scheduled"] for bucket in platforms.values()),
        next_scheduled_at=upcoming[0].scheduled_at if upcoming else None,
        published_this_month=published_this_month,
        published_platforms=sorted(published_platforms),
    )

    return DashboardResponse(
        stats=stats,
        platforms=[PlatformSummary(platform=name, **counts) for name, counts in platforms.items()],
        top_post=top_post,
        recent_activity=activity[:ACTIVITY_LIMIT],
        coming_up=upcoming[:COMING_UP_LIMIT],
    )
