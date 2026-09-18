import re
from datetime import date as date_cls
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.post import GeneratedPost
from app.models.blog import GeneratedBlog
from app.schemas.calendar import CalendarItem, CalendarResponse, RescheduleRequest
from app.services.scheduler_service import parse_scheduled_datetime

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

_TAG_RE = re.compile(r"<[^>]+>")
PREVIEW_LENGTH = 160


def _preview_text(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"\s{2,}", " ", _TAG_RE.sub(" ", raw)).strip()
    return text[:PREVIEW_LENGTH] if text else None


def _parse_bound(value: str | None, field: str) -> date_cls | None:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be in YYYY-MM-DD format.",
        )


def _to_calendar_item(kind: str, record: Any, scheduled_at) -> CalendarItem:
    return CalendarItem(
        id=record.id,
        content_type=kind,
        title=record.title or getattr(record, "headline", None),
        preview=_preview_text(getattr(record, "caption", None) or getattr(record, "content", None)),
        image_url=record.image_url,
        platform=record.platform,
        date=record.date,
        start_time=record.start_time,
        scheduled_at=scheduled_at,
        is_posted=record.is_posted,
        posted_at=record.posted_at,
        post_error=record.post_error,
        language=record.language,
        ai_safety_score=record.ai_safety_score,
    )


@router.get(
    "",
    response_model=CalendarResponse,
    summary="Approved posts and blogs placed on the calendar for a date range",
)
def get_calendar(
    start_date: Optional[str] = Query(None, description="Range start (YYYY-MM-DD). Open-ended when omitted."),
    end_date: Optional[str] = Query(None, description="Range end, inclusive (YYYY-MM-DD). Open-ended when omitted."),
    content_type: Optional[str] = Query(None, description="Limit to 'post' or 'blog'; both are returned by default"),
    platform: Optional[str] = Query(None, description="Limit to a single platform, e.g. linkedin"),
    item_status: Optional[str] = Query(
        None,
        alias="status",
        description="'scheduled' for items still waiting to go out, 'published' for ones already sent",
    ),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Everything approved and dated for the Day, Week and Month views: pass the range
    the view is showing and each item comes back with the date and time to place it at.
    """
    wanted_type = (content_type or "").strip().lower()
    if wanted_type and wanted_type not in {"post", "blog"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="content_type must be either 'post' or 'blog'.",
        )

    wanted_status = (item_status or "").strip().lower()
    if wanted_status and wanted_status not in {"scheduled", "published"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status must be either 'scheduled' or 'published'.",
        )

    range_start = _parse_bound(start_date, "start_date")
    range_end = _parse_bound(end_date, "end_date")
    if range_start and range_end and range_start > range_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date cannot be after end_date.",
        )

    company = resolve_company(db, auth, company_id)
    wanted_platform = (platform or "").strip().lower()

    records: List[tuple] = []
    if wanted_type in ("", "post"):
        posts = (
            db.query(GeneratedPost)
            .filter(
                GeneratedPost.company_id == company.id,
                GeneratedPost.is_approved == True,  # noqa: E712
            )
            .all()
        )
        records += [("post", post) for post in posts]

    if wanted_type in ("", "blog"):
        blogs = (
            db.query(GeneratedBlog)
            .filter(
                GeneratedBlog.company_id == company.id,
                GeneratedBlog.is_approved == True,  # noqa: E712
            )
            .all()
        )
        records += [("blog", blog) for blog in blogs]

    items: List[CalendarItem] = []
    unscheduled = 0

    for kind, record in records:
        if wanted_platform and (record.platform or "").lower() != wanted_platform:
            continue

        scheduled_at = parse_scheduled_datetime(record.date, record.start_time)
        if not scheduled_at:
            unscheduled += 1
            continue

        day = scheduled_at.date()
        if range_start and day < range_start:
            continue
        if range_end and day > range_end:
            continue

        if wanted_status == "scheduled" and record.is_posted:
            continue
        if wanted_status == "published" and not record.is_posted:
            continue

        items.append(_to_calendar_item(kind, record, scheduled_at))

    items.sort(key=lambda item: item.scheduled_at)
    published = sum(1 for item in items if item.is_posted)

    return CalendarResponse(
        items=items,
        total=len(items),
        scheduled=len(items) - published,
        published=published,
        unscheduled=unscheduled,
    )


@router.patch(
    "/{content_type}/{item_id}/schedule",
    response_model=CalendarItem,
    summary="Move a calendar item to a new date and time",
)
def reschedule_item(
    content_type: str,
    item_id: str,
    payload: RescheduleRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Used when an item is dragged to another slot. Already published items keep their
    original slot, since moving them would not change anything that has gone out.
    """
    kind = content_type.strip().lower()
    if kind not in {"post", "blog"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="content_type must be either 'post' or 'blog'.",
        )

    company = resolve_company(db, auth, company_id)
    model = GeneratedPost if kind == "post" else GeneratedBlog
    record = db.query(model).filter(model.id == item_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No {kind} found with id '{item_id}'.")
    if record.company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This {kind} does not belong to your account.",
        )
    if record.is_posted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This {kind} has already been published and cannot be rescheduled.",
        )

    scheduled_at = parse_scheduled_datetime(payload.date, payload.start_time)
    if not scheduled_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not read the new schedule. Use date as YYYY-MM-DD and start_time as HH:MM.",
        )

    record.date = payload.date
    record.start_time = payload.start_time
    db.commit()
    db.refresh(record)

    return _to_calendar_item(kind, record, scheduled_at)
