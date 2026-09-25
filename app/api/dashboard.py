import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.blog import GeneratedBlog
from app.models.companies import Company, Role
from app.models.plan import SubscriptionPlan
from app.models.library import Library
from app.models.post import GeneratedPost
from app.schemas.dashboard import (
    ActivityItem,
    AdminCompanyRow,
    SuperAdminDashboardResponse,
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


def _approval_activity(batch: list, approved_at: datetime) -> ActivityItem:
    kinds = {kind for kind, _, _, _ in batch}
    platforms = sorted({platform for _, _, platform, _ in batch})
    if len(batch) == 1:
        kind, record, platform, title = batch[0]
        return ActivityItem(
            type="approved",
            message=f"Approved {kind} for {platform}: {title}",
            content_type=kind,
            item_id=record.id,
            platform=platform,
            at=approved_at,
        )

    noun = f"{next(iter(kinds))}s" if len(kinds) == 1 else "items"
    return ActivityItem(
        type="approved",
        message=f"Approved {len(batch)} {noun} for {', '.join(platforms)}",
        content_type=next(iter(kinds)) if len(kinds) == 1 else None,
        platform=platforms[0] if len(platforms) == 1 else None,
        at=approved_at,
    )


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
    approval_batches: dict = {}

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
        approved_at = _as_utc(record.approved_at)
        if approved_at:
            approval_batches.setdefault(approved_at, []).append((kind, record, platform, title))

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

    # Everything approved in one action shares a timestamp, so one batch is one line.
    for approved_at, batch in approval_batches.items():
        activity.append(_approval_activity(batch, approved_at))

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


@router.get(
    "/super-admin",
    response_model=SuperAdminDashboardResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="Company, revenue and plan totals for the Super Admin dashboard",
)
def get_super_admin_dashboard(
    recent_limit: int = Query(5, ge=1, le=50, description="How many rows for Recent Companies"),
    db: Session = Depends(deps.get_db),
) -> Any:
    """
    Revenue is what the plans in force are worth per month, so a yearly plan counts
    as a twelfth of what was paid. Super admin accounts are left out of the totals.
    """
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    companies = (
        db.query(Company)
        .filter(Company.role != Role.SUPERADMIN)
        .order_by(Company.created_at.desc())
        .all()
    )
    plans_by_code = {plan.code: plan for plan in db.query(SubscriptionPlan).all()}

    monthly_revenue = 0.0
    paid = 0
    rows: List[AdminCompanyRow] = []

    for company in companies:
        plan = company.plan
        code = plan["plan_code"]
        is_paid = code != "free"
        if is_paid:
            paid += 1
            saved = plans_by_code.get(code)
            amount = plan.get("amount") or 0.0
            is_yearly = "yearly" in (plan.get("plan_name") or "").lower()
            if saved:
                monthly_revenue += saved.yearly_price / 12 if is_yearly else saved.monthly_price
            else:
                monthly_revenue += amount / 12 if is_yearly else amount

        joined = _as_utc(company.created_at)
        rows.append(
            AdminCompanyRow(
                id=company.id,
                name=company.name,
                email=company.email,
                avatar_url=company.avatar_url,
                plan_code=code,
                plan_name=plan["plan_name"],
                joined_at=joined,
                is_active=company.is_active,
                status="Active" if company.is_active else "Suspended",
            )
        )

    active = sum(1 for c in companies if c.is_active)
    new_this_week = sum(1 for c in companies if (_as_utc(c.created_at) or now) >= week_ago)

    return SuperAdminDashboardResponse(
        total_companies=len(companies),
        new_companies_this_week=new_this_week,
        monthly_revenue=round(monthly_revenue, 2),
        paid_plans=paid,
        free_plans=len(companies) - paid,
        active_companies=active,
        suspended_companies=len(companies) - active,
        recent_companies=rows[:recent_limit],
    )
