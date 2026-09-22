import calendar as calendar_module
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.post import GeneratedPost
from app.schemas.planner import (
    GeneratePlanRequest,
    GeneratePlanResponse,
    PlannerItem,
    PlannerQueueResponse,
)
from app.services.planner_service import generate_content_plan
from app.services.notification_service import notify, titles_summary

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

SUPPORTED_PLATFORMS = {"linkedin", "instagram", "facebook", "x"}

# A month of daily posts is the most one plan can hold; it also keeps the single
# planning call to a sane size.
MAX_PLAN_ITEMS = 31


def _parse_platforms(platforms: str) -> List[str]:
    result = []
    for raw in platforms.replace(";", ",").split(","):
        name = raw.strip().lower()
        if name == "twitter":
            name = "x"
        if name and name not in result:
            result.append(name)

    unsupported = [p for p in result if p not in SUPPORTED_PLATFORMS]
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform(s): {', '.join(unsupported)}. Supported: linkedin, instagram, facebook, x.",
        )
    if not result:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one platform must be selected.")
    return result


def _parse_date(value: str | None, field: str) -> date_cls | None:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be in YYYY-MM-DD format.",
        )


def _period_bounds(period: str, start: date_cls) -> tuple[date_cls, date_cls, int]:
    if period == "month":
        days = calendar_module.monthrange(start.year, start.month)[1]
    else:
        days = 7
    return start, start + timedelta(days=days - 1), days


@router.post(
    "/generate",
    response_model=GeneratePlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Plan a week or month of posts in one go",
)
def generate_plan(
    payload: GeneratePlanRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Writes a whole run of posts in a single model call and spreads them across the
    period, one slot per day. In auto_schedule mode the plan is approved so the
    scheduler publishes it; in manual_approval mode it lands in the Approval Queue.
    """
    company = resolve_company(db, auth, company_id)
    target_platforms = _parse_platforms(payload.platforms)

    start = _parse_date(payload.start_date, "start_date") or (date_cls.today() + timedelta(days=1))
    start, end, period_days = _period_bounds(payload.period, start)

    requested = payload.count or period_days
    count = max(1, min(requested, MAX_PLAN_ITEMS))

    plan = generate_content_plan(
        count=count,
        platforms=target_platforms,
        language=payload.language,
        topic=payload.topic,
        company_description=payload.company_description,
        brand_tone=payload.brand_tone,
        target_audience=payload.target_audience,
    )

    approved = payload.mode == "auto_schedule"
    approved_at = datetime.now(timezone.utc) if approved else None
    created: List[GeneratedPost] = []

    for index, entry in enumerate(plan):
        slot_date = start + timedelta(days=index % period_days)
        platform = target_platforms[index % len(target_platforms)]
        post = GeneratedPost(
            company_id=company.id,
            prompt=payload.topic or "Planned by the content planner",
            platform=platform,
            title=entry["headline"],
            headline=entry["headline"],
            caption=entry["caption"],
            hashtags=entry["hashtags"],
            date=slot_date.isoformat(),
            start_time=payload.post_time,
            tone=payload.brand_tone or "Professional",
            language=payload.language,
            ai_safety_score=entry["ai_safety_score"],
            is_approved=approved,
            approved_at=approved_at,
            is_posted=False,
        )
        db.add(post)
        created.append(post)

    db.commit()
    for post in created:
        db.refresh(post)

    if not approved:
        notify(
            db,
            company.id,
            "ready_for_approval",
            f"The planner drafted {len(created)} post(s) for {start.isoformat()} to {end.isoformat()}, "
            f"ready for approval: {titles_summary([p.title for p in created])}",
        )

    note = None
    if requested > count:
        note = f"Requested {requested} posts, planned {count} to stay within the {MAX_PLAN_ITEMS} post limit."

    return GeneratePlanResponse(
        status="success",
        period=payload.period,
        mode=payload.mode,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        count=len(created),
        items=[PlannerItem.model_validate(post) for post in created],
        note=note,
    )


@router.get(
    "",
    response_model=PlannerQueueResponse,
    summary="The planned queue for an upcoming week or month",
)
def get_planner_queue(
    period: str = Query("week", description="'week' or 'month'"),
    start_date: Optional[str] = Query(None, description="First day of the period (YYYY-MM-DD). Defaults to tomorrow."),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    What the planner screen shows after a plan is generated: every planned post in the
    period, whether it is still awaiting approval, scheduled, or already published.
    """
    wanted = period.strip().lower()
    if wanted not in {"week", "month"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="period must be either 'week' or 'month'.")

    company = resolve_company(db, auth, company_id)
    start = _parse_date(start_date, "start_date") or (date_cls.today() + timedelta(days=1))
    start, end, _ = _period_bounds(wanted, start)

    posts = (
        db.query(GeneratedPost)
        .filter(
            GeneratedPost.company_id == company.id,
            GeneratedPost.date >= start.isoformat(),
            GeneratedPost.date <= end.isoformat(),
        )
        .order_by(GeneratedPost.date.asc(), GeneratedPost.start_time.asc())
        .all()
    )

    items = [PlannerItem.model_validate(post) for post in posts]
    published = sum(1 for item in items if item.is_posted)
    awaiting = sum(1 for item in items if not item.is_approved and not item.is_posted)

    return PlannerQueueResponse(
        items=items,
        total=len(items),
        awaiting_approval=awaiting,
        scheduled=len(items) - published - awaiting,
        published=published,
    )
