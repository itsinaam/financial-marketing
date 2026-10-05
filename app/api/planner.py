import calendar as calendar_module
import json
import logging
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

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
from app.services.post_generator_service import create_generated_post, extract_post_plan_from_pdf
from app.services.brand_service import brand_prompt_context, get_brand_profile
from app.services.notification_service import notify, titles_summary

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = {"linkedin", "instagram", "facebook", "x"}

# Keep planner image generation within the same per-request limit as post generation.
MAX_PLAN_ITEMS = 10
MAX_PLANNER_PDF_BYTES = 4 * 1024 * 1024

_PLAN_FIELDS = {
    "period",
    "start_date",
    "platforms",
    "count",
    "post_time",
    "mode",
    "topic",
    "company_description",
    "brand_tone",
    "target_audience",
    "language",
}

_PLAN_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "period": {"type": "string", "enum": ["week", "month"], "default": "week"},
        "start_date": {"type": "string", "format": "date"},
        "platforms": {"type": "string", "default": "linkedin"},
        "count": {"type": "integer", "minimum": 1, "maximum": MAX_PLAN_ITEMS},
        "post_time": {"type": "string", "default": "09:00"},
        "mode": {"type": "string", "enum": ["auto_schedule", "manual_approval"], "default": "manual_approval"},
        "topic": {"type": "string"},
        "company_description": {"type": "string"},
        "brand_tone": {"type": "string"},
        "target_audience": {"type": "string"},
        "language": {"type": "string", "default": "English (US)"},
    },
}


async def _read_plan_request(request: Request) -> tuple[GeneratePlanRequest, bytes | None]:
    content_type = request.headers.get("content-type", "").lower()
    pdf_bytes = None

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        raw_payload = form.get("payload")
        if raw_payload is not None:
            try:
                payload_data = json.loads(str(raw_payload))
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=422, detail="The 'payload' form field must contain valid JSON.") from error
        else:
            payload_data = {}
            for field in _PLAN_FIELDS:
                value = form.get(field)
                if value is None:
                    continue
                if isinstance(value, str):
                    value = value.strip()
                    if not value:
                        continue
                payload_data[field] = value

        upload = form.get("planner_pdf")
        if upload is not None:
            if not isinstance(upload, StarletteUploadFile):
                raise HTTPException(status_code=422, detail="planner_pdf must be an uploaded PDF file.")
            if upload.filename:
                if not upload.filename.lower().endswith(".pdf") or upload.content_type not in {
                    "application/pdf",
                    "application/octet-stream",
                }:
                    raise HTTPException(status_code=415, detail="The planner upload must be a PDF file.")
                pdf_bytes = await upload.read(MAX_PLANNER_PDF_BYTES + 1)
                if not pdf_bytes:
                    raise HTTPException(status_code=400, detail="The planner PDF is empty.")
                if len(pdf_bytes) > MAX_PLANNER_PDF_BYTES:
                    raise HTTPException(status_code=413, detail="The planner PDF must be 4 MiB or smaller.")
                if b"%PDF-" not in pdf_bytes[:1024]:
                    raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF.")
    else:
        try:
            payload_data = await request.json()
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="Send a JSON planner request or multipart form data.") from error

    if not isinstance(payload_data, dict):
        raise HTTPException(status_code=422, detail="Planner request data must be a JSON object.")
    try:
        payload = GeneratePlanRequest.model_validate(payload_data)
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error

    return payload, pdf_bytes


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
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            **_PLAN_REQUEST_SCHEMA["properties"],
                            "planner_pdf": {"type": "string", "format": "binary"},
                            "payload": {"type": "string", "description": "Optional JSON-encoded GeneratePlanRequest"},
                        },
                    }
                },
            },
        }
    },
)
async def generate_plan(
    request: Request,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Writes a whole run of posts in a single model call and spreads them across the
    period, one slot per day. In auto_schedule mode the plan is approved so the
    scheduler publishes it; in manual_approval mode it lands in the Approval Queue.
    """
    payload, planner_pdf_bytes = await _read_plan_request(request)
    company = resolve_company(db, auth, company_id)
    target_platforms = _parse_platforms(payload.platforms)

    start = _parse_date(payload.start_date, "start_date") or (date_cls.today() + timedelta(days=1))
    start, end, period_days = _period_bounds(payload.period, start)

    planned_entries = None
    if planner_pdf_bytes is not None:
        try:
            planned_entries = extract_post_plan_from_pdf(planner_pdf_bytes)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            logger.exception("Gemini failed while extracting the planner PDF")
            raise HTTPException(
                status_code=502,
                detail="The planner PDF could not be processed. Please try again.",
            ) from error

    requested = (
        payload.count
        if payload.count is not None
        else len(planned_entries) if planned_entries is not None else period_days
    )
    count = max(1, min(requested, MAX_PLAN_ITEMS))
    planned_entries = planned_entries[:count] if planned_entries is not None else None

    # Anything the request leaves out comes from the brand profile set in Themes.
    brand = get_brand_profile(db, company.id)
    plan = generate_content_plan(
        count=count,
        platforms=target_platforms,
        language=payload.language,
        topic=payload.topic,
        planned_topics=(
            [
                f"{entry['topic']}\nAdditional instructions: {payload.topic.strip()}"
                if payload.topic and payload.topic.strip()
                else entry["topic"]
                for entry in planned_entries
            ]
            if planned_entries is not None
            else None
        ),
        company_description=payload.company_description or (brand.company_description if brand else None),
        brand_tone=payload.brand_tone or (brand.brand_tone if brand else None),
        target_audience=payload.target_audience or (brand.target_audience if brand else None),
        brand_context=brand_prompt_context(
            brand,
            company_description=payload.company_description,
            target_audience=payload.target_audience,
        ),
    )

    approved = payload.mode == "auto_schedule"
    approved_at = datetime.now(timezone.utc) if approved else None
    created: List[GeneratedPost] = []

    for index, entry in enumerate(plan):
        pdf_entry = (
            planned_entries[index]
            if planned_entries is not None and index < len(planned_entries)
            else None
        )
        slot_date = _parse_date(pdf_entry["date"], "PDF post date") if pdf_entry else None
        slot_date = slot_date or (start + timedelta(days=index % period_days))
        platform = target_platforms[index % len(target_platforms)]
        post_prompt = (pdf_entry["topic"] if pdf_entry else None) or payload.topic or entry["headline"]
        if pdf_entry and payload.topic and payload.topic.strip():
            post_prompt = f"{post_prompt}\nAdditional instructions: {payload.topic.strip()}"
        post = create_generated_post(
            db=db,
            company_id=company.id,
            prompt=post_prompt,
            platform=platform,
            tone=payload.brand_tone or (brand.brand_tone if brand else None),
            language=payload.language,
            date=slot_date.isoformat(),
            start_time=(pdf_entry.get("start_time") if pdf_entry else None) or payload.post_time,
            is_approved=approved,
            approved_at=approved_at,
            generated_content=entry,
        )
        created.append(post)

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
        source = "Requested"
        note = f"{source} {requested} posts; planned {count} to stay within the {MAX_PLAN_ITEMS} post limit."

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
