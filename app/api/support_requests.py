import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import deps
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.companies import Company
from app.models.support_request import SupportRequest, SupportRequestEvent
from app.schemas.support_request import (
    CompanyRequestSummary,
    DeleteSupportRequests,
    OpenCountResponse,
    SupportRequestDetailResponse,
    SupportRequestEventResponse,
    UpdateStatusResponse,
    UpdateSupportRequestStatus,
    UpdateSupportRequestsStatus,
    DeleteSupportRequestsResponse,
    SubmitSupportRequest,
    SupportRequestResponse,
    SupportRequestsResponse,
)
from app.services.referral_service import _send_email

router = APIRouter()
logger = logging.getLogger("SupportRequests")


def _record(session: Session, request_id: int, kind: str, detail: str | None = None, actor: str | None = None) -> None:
    """
    Add a line to a request's history. Never raises: a missing history line is
    not worth failing the thing it was describing.
    """
    try:
        session.add(SupportRequestEvent(request_id=request_id, kind=kind, detail=detail, actor=actor))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.warning("Could not record '%s' on request %s: %s", kind, request_id, exc)


def _support_inbox() -> str:
    """Where a support request is emailed. Falls back to the Super Admin's address."""
    return (settings.SUPPORT_EMAIL or settings.SUPERADMIN_EMAIL or "").strip()


def _notify_support(
    request_id: int,
    name: str,
    email: str,
    message: str,
    company_label: str,
    company_email: str,
    company_id: int,
    received: str,
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

    company_line = f"{company_label} <{company_email}> (id {company_id})" if company_email else f"{company_label} (id {company_id})"
    body = (
        "New support request\n"
        "\n"
        f"From:      {name} <{email}>\n"
        f"Company:   {company_line}\n"
        f"Received:  {received}\n"
        f"Reference: #{request_id}\n"
        "\n"
        "Message\n"
        "-------\n"
        f"{message}\n"
        "\n"
        "---\n"
        f"Reply to this email to answer {name} directly.\n"
    )
    try:
        _send_email(
            recipient,
            f"Support request from {name} ({company_label})",
            body,
            reply_to=email,
        )
    except Exception as exc:  # noqa: BLE001 - mail must never take anything else down
        logger.warning("Could not email support request %s: %s", request_id, exc)
        session = SessionLocal()
        try:
            _record(session, request_id, "support_email_failed", f"Could not reach {recipient}")
        finally:
            session.close()
        return

    session = SessionLocal()
    try:
        stored = session.query(SupportRequest).filter(SupportRequest.id == request_id).first()
        if stored is not None:
            stored.email_sent = True
            session.commit()
        _record(session, request_id, "support_emailed", f"Sent to {recipient}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Emailed support request %s but could not mark it: %s", request_id, exc)
    finally:
        session.close()


def _notify_resolved(request_id: int, name: str, email: str, message: str, received: str) -> None:
    """
    Tell whoever wrote in that their request has been closed.

    Runs after the response for the same reason the first email does, and a
    failure is only logged: the request is closed either way, and there is
    nobody left to report it to. The reply address is the support inbox, so if
    it turns out not to be sorted their answer comes back to the right place.
    """
    body = (
        f"Hello {name},\n"
        "\n"
        "Your support request has been marked resolved.\n"
        "\n"
        f"Reference: #{request_id}\n"
        f"Sent:      {received}\n"
        "\n"
        "Your message\n"
        "------------\n"
        f"{message}\n"
        "\n"
        "---\n"
        "If this still is not sorted, reply to this email and we will pick it up again.\n"
    )
    session = SessionLocal()
    try:
        _send_email(email, "Your support request has been resolved", body, reply_to=_support_inbox() or None)
        _record(session, request_id, "resolved_emailed", f"Sent to {email}")
    except Exception as exc:  # noqa: BLE001 - the request is closed regardless
        logger.warning("Could not tell %s that request %s was resolved: %s", email, request_id, exc)
        _record(session, request_id, "resolved_email_failed", f"Could not reach {email}")
    finally:
        session.close()


def _received_label(value) -> str:
    return (value or datetime.now(timezone.utc)).strftime("%d %b %Y, %H:%M UTC")


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
    _record(db, request.id, "created", f"Sent through the Support form by {request.name}")

    background.add_task(
        _notify_support,
        request.id,
        request.name,
        request.email,
        request.message,
        current_user.name or current_user.email,
        current_user.email,
        current_user.id,
        (request.created_at or datetime.now(timezone.utc)).strftime("%d %b %Y, %H:%M UTC"),
    )

    return SupportRequestResponse(
        id=request.id,
        company_id=request.company_id,
        company_name=current_user.name or current_user.email,
        name=request.name,
        email=request.email,
        message=request.message,
        email_sent=request.email_sent,
        status=request.status,
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
            status=r.status,
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


@router.patch(
    "/{request_id}",
    response_model=SupportRequestResponse,
    summary="Open or close one support request (Super Admin only)",
)
def set_support_request_status(
    request_id: int,
    payload: UpdateSupportRequestStatus,
    background: BackgroundTasks,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_superadmin),
) -> Any:
    request = db.query(SupportRequest).filter(SupportRequest.id == request_id).first()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No support request with id {request_id}.",
        )

    # Only the move into closed is worth an email; closing something already
    # closed, or reopening it, is not news to the person who wrote in.
    newly_closed = payload.status == "closed" and request.status != "closed"
    if newly_closed:
        background.add_task(
            _notify_resolved,
            request.id,
            request.name,
            request.email,
            request.message,
            _received_label(request.created_at),
        )

    if request.status != payload.status:
        _record(
            db,
            request.id,
            "closed" if payload.status == "closed" else "reopened",
            None,
            current_user.name or current_user.email,
        )

    request.status = payload.status
    db.commit()
    db.refresh(request)

    company = (
        db.query(Company).filter(Company.id == request.company_id).first()
        if request.company_id
        else None
    )
    return SupportRequestResponse(
        id=request.id,
        company_id=request.company_id,
        company_name=(company.name or company.email) if company else None,
        name=request.name,
        email=request.email,
        message=request.message,
        email_sent=request.email_sent,
        status=request.status,
        created_at=request.created_at,
    )


@router.post(
    "/status",
    response_model=UpdateStatusResponse,
    summary="Open or close several support requests at once (Super Admin only)",
)
def set_support_requests_status(
    payload: UpdateSupportRequestsStatus,
    background: BackgroundTasks,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_superadmin),
) -> Any:
    if payload.status == "closed":
        # Read the ones actually changing before the update, so closing a list
        # that is already half closed does not email the same people twice.
        becoming_closed = (
            db.query(SupportRequest)
            .filter(SupportRequest.id.in_(payload.ids), SupportRequest.status != "closed")
            .all()
        )
        for request in becoming_closed:
            background.add_task(
                _notify_resolved,
                request.id,
                request.name,
                request.email,
                request.message,
                _received_label(request.created_at),
            )

    # Read which ones actually move before updating, so the history only gains a
    # line where something really changed.
    changing = [
        row.id
        for row in db.query(SupportRequest)
        .filter(SupportRequest.id.in_(payload.ids), SupportRequest.status != payload.status)
        .all()
    ]

    updated = (
        db.query(SupportRequest)
        .filter(SupportRequest.id.in_(payload.ids))
        .update({SupportRequest.status: payload.status}, synchronize_session=False)
    )
    db.commit()

    actor = current_user.name or current_user.email
    kind = "closed" if payload.status == "closed" else "reopened"
    for request_id in changing:
        _record(db, request_id, kind, "Changed with others", actor)

    return UpdateStatusResponse(updated=updated or 0)


@router.get(
    "/open-count",
    response_model=OpenCountResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="How many support requests are still open (Super Admin only)",
)
def open_support_request_count(
    db: Session = Depends(deps.get_db),
) -> Any:
    """
    Declared before /{request_id} so the path is not read as an id, and kept to a
    count because the sidebar asks for it on a timer.
    """
    open_count = (
        db.query(func.count(SupportRequest.id)).filter(SupportRequest.status != "closed").scalar()
    )
    return OpenCountResponse(open=open_count or 0)


@router.get(
    "/{request_id}",
    response_model=SupportRequestDetailResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="One support request with everything that has happened to it (Super Admin only)",
)
def get_support_request(
    request_id: int,
    db: Session = Depends(deps.get_db),
) -> Any:
    request = db.query(SupportRequest).filter(SupportRequest.id == request_id).first()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No support request with id {request_id}.",
        )

    company = (
        db.query(Company).filter(Company.id == request.company_id).first()
        if request.company_id
        else None
    )
    events = (
        db.query(SupportRequestEvent)
        .filter(SupportRequestEvent.request_id == request.id)
        .order_by(SupportRequestEvent.id.asc())
        .all()
    )

    company_requests = []
    if request.company_id:
        company_requests = (
            db.query(SupportRequest)
            .filter(SupportRequest.company_id == request.company_id)
            .order_by(SupportRequest.id.desc())
            .limit(50)
            .all()
        )

    return SupportRequestDetailResponse(
        id=request.id,
        company_id=request.company_id,
        company_name=(company.name or company.email) if company else None,
        name=request.name,
        email=request.email,
        message=request.message,
        email_sent=request.email_sent,
        status=request.status,
        created_at=request.created_at,
        events=[SupportRequestEventResponse.model_validate(e) for e in events],
        company_requests=[CompanyRequestSummary.model_validate(r) for r in company_requests],
    )
