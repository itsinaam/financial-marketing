import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core import deps
from app.core.config import settings
from app.models.companies import Company
from app.models.support_request import SupportRequest
from app.schemas.support_request import (
    SubmitSupportRequest,
    SupportRequestResponse,
    SupportRequestsResponse,
)
from app.services.referral_service import _send_email

router = APIRouter()
logger = logging.getLogger("SupportRequests")


def _support_inbox() -> str:
    """Where a support request is emailed. Falls back to the Super Admin's address."""
    return (settings.SUPPORT_EMAIL or settings.SUPERADMIN_EMAIL or "").strip()


def _notify_support(request: SupportRequest, company: Company) -> bool:
    """
    Email the request on. Returns whether it went out; a failure is logged and
    swallowed because the request is already saved and must not be lost over it.
    """
    recipient = _support_inbox()
    if not recipient:
        logger.warning("No support inbox configured; set SUPPORT_EMAIL or SUPERADMIN_EMAIL.")
        return False

    body = (
        f"New support request from {request.name} <{request.email}>\n"
        f"Company: {company.name or company.email} (id {company.id})\n\n"
        f"{request.message}\n"
    )
    try:
        _send_email(recipient, f"Support request from {request.name}", body)
        return True
    except Exception as exc:  # noqa: BLE001 - mail must never fail the request
        logger.warning("Could not email support request %s: %s", request.id, exc)
        return False


@router.post(
    "/",
    response_model=SupportRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message through the Support form",
)
def submit_support_request(
    payload: SubmitSupportRequest,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Stored first and emailed after, so the Super Admin's list is the record of
    truth even when mail is down.
    """
    request = SupportRequest(
        company_id=current_user.id,
        name=payload.name.strip(),
        email=str(payload.email).strip(),
        message=payload.message.strip(),
    )
    db.add(request)
    db.commit()
    db.refresh(request)

    sent = _notify_support(request, current_user)
    if sent:
        request.email_sent = True
        db.commit()
        db.refresh(request)

    return SupportRequestResponse(
        id=request.id,
        company_id=request.company_id,
        company_name=current_user.name or current_user.email,
        name=request.name,
        email=request.email,
        message=request.message,
        email_sent=request.email_sent,
        created_at=request.created_at,
    )


@router.get(
    "/",
    response_model=SupportRequestsResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="Every Support form submission, newest first (Super Admin only)",
)
def list_support_requests(
    limit: int = Query(200, ge=1, le=500, description="How many of the most recent to return"),
    db: Session = Depends(deps.get_db),
) -> Any:
    rows: List[SupportRequest] = (
        db.query(SupportRequest).order_by(SupportRequest.id.desc()).limit(limit).all()
    )
    total = db.query(SupportRequest).count()

    company_ids = {r.company_id for r in rows if r.company_id is not None}
    companies: Dict[int, Company] = {}
    if company_ids:
        companies = {
            company.id: company
            for company in db.query(Company).filter(Company.id.in_(company_ids)).all()
        }

    requests = [
        SupportRequestResponse(
            id=r.id,
            company_id=r.company_id,
            company_name=(
                (companies[r.company_id].name or companies[r.company_id].email)
                if r.company_id in companies
                else None
            ),
            name=r.name,
            email=r.email,
            message=r.message,
            email_sent=r.email_sent,
            created_at=r.created_at,
        )
        for r in rows
    ]

    return SupportRequestsResponse(requests=requests, total=total)
