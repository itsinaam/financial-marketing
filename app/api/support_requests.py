import logging
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import deps
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.companies import Company
from app.models.support_request import SupportRequest
from app.schemas.support_request import (
    DeleteSupportRequests,
    DeleteSupportRequestsResponse,
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


def _notify_support(
    request_id: int,
    name: str,
    email: str,
    message: str,
    company_label: str,
    company_id: int,
) -> None:
    """
    Email the request on and record whether it went out.

    This runs after the response has been sent. Talking to a mail server takes
    seconds, and nobody should wait on that to be told their message arrived -
    it is already saved by then. A failure is logged and the row simply stays at
    "Not sent", because by now there is no one left to raise it to.
    """
    recipient = _support_inbox()
    if not recipient:
        logger.warning("No support inbox configured; set SUPPORT_EMAIL or SUPERADMIN_EMAIL.")
        return

    body = (
        f"New support request from {name} <{email}>\n"
        f"Company: {company_label} (id {company_id})\n\n"
        f"{message}\n"
    )
    try:
        _send_email(recipient, f"Support request from {name}", body)
    except Exception as exc:  # noqa: BLE001 - mail must never take anything else down
        logger.warning("Could not email support request %s: %s", request_id, exc)
        return

    session = SessionLocal()
    try:
        stored = session.query(SupportRequest).filter(SupportRequest.id == request_id).first()
        if stored is not None:
            stored.email_sent = True
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Emailed support request %s but could not mark it: %s", request_id, exc)
    finally:
        session.close()


@router.post(
    "/",
    response_model=SupportRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message through the Support form",
)
def submit_support_request(
    payload: SubmitSupportRequest,
    background: BackgroundTasks,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Stored first and emailed once the response has gone out, so the Super
    Admin's list is the record of truth even when mail is slow or down. The row
    comes back as not yet emailed; the list the Super Admin watches picks the
    change up on its next read.
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

    background.add_task(
        _notify_support,
        request.id,
        request.name,
        request.email,
        request.message,
        current_user.name or current_user.email,
        current_user.id,
    )

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


@router.delete(
    "/{request_id}",
    response_model=DeleteSupportRequestsResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="Delete one support request (Super Admin only)",
)
def delete_support_request(
    request_id: int,
    db: Session = Depends(deps.get_db),
) -> Any:
    request = db.query(SupportRequest).filter(SupportRequest.id == request_id).first()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No support request with id {request_id}.",
        )
    db.delete(request)
    db.commit()
    return DeleteSupportRequestsResponse(deleted=1)


@router.post(
    "/delete",
    response_model=DeleteSupportRequestsResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="Delete several support requests at once (Super Admin only)",
)
def delete_support_requests(
    payload: DeleteSupportRequests,
    db: Session = Depends(deps.get_db),
) -> Any:
    """
    Ids that no longer exist are simply not counted, so clearing a list that
    somebody else already emptied is not an error.
    """
    deleted = (
        db.query(SupportRequest)
        .filter(SupportRequest.id.in_(payload.ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return DeleteSupportRequestsResponse(deleted=deleted or 0)
