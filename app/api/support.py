from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from jose import JWTError, jwt
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import deps
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.companies import Company, Role
from app.models.support import SupportMessage
from app.schemas.support import (
    MarkReadResponse,
    SendSupportMessageRequest,
    SupportConversationRow,
    SupportConversationsResponse,
    SupportMessageResponse,
    SupportThreadResponse,
    UnreadCountResponse,
)
from app.services.support_ws import support_manager

router = APIRouter()

COMPANY_ROLE = "company"
ADMIN_ROLE = "superadmin"
# What a company sees as the name on the Support side of the chat.
SUPPORT_DISPLAY_NAME = "Support"
MAX_BODY_LENGTH = 4000


def is_admin(user: Company) -> bool:
    return user.role == Role.SUPERADMIN or bool(user.is_superuser)


def sender_role_of(user: Company) -> str:
    return ADMIN_ROLE if is_admin(user) else COMPANY_ROLE


def _thread_company(
    db: Session,
    current_user: Company,
    company_id: Optional[int],
) -> Company:
    """
    The company whose thread is being read or written to.

    A Super Admin must name the company; a company can only ever reach its own
    thread, so passing somebody else's id is rejected rather than ignored.
    """
    if is_admin(current_user):
        if company_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="company_id is required: a Super Admin opens one company's chat at a time.",
            )
        target = db.query(Company).filter(Company.id == company_id).first()
        if target is None or target.role == Role.SUPERADMIN:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No company with id {company_id}.",
            )
        return target

    if company_id is not None and company_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only open your own Support chat.",
        )
    return current_user


def _to_response(message: SupportMessage, company: Company) -> SupportMessageResponse:
    name = SUPPORT_DISPLAY_NAME if message.sender_role == ADMIN_ROLE else (company.name or company.email)
    return SupportMessageResponse(
        id=message.id,
        company_id=message.company_id,
        sender_id=message.sender_id,
        sender_role=message.sender_role,
        sender_name=name,
        body=message.body,
        read_at=message.read_at,
        created_at=message.created_at,
    )


def _unread_for(db: Session, user: Company) -> int:
    """
    A company counts what Support has sent it. A Super Admin counts everything
    companies have sent across every thread, because the badge is global for them.
    """
    query = db.query(func.count(SupportMessage.id)).filter(SupportMessage.read_at.is_(None))
    if is_admin(user):
        return query.filter(SupportMessage.sender_role == COMPANY_ROLE).scalar() or 0
    return (
        query.filter(
            SupportMessage.company_id == user.id,
            SupportMessage.sender_role == ADMIN_ROLE,
        ).scalar()
        or 0
    )


def _mark_thread_read(db: Session, company_id: int, reader_role: str) -> int:
    """Stamp read_at on whatever the *other* side wrote in this thread."""
    other_side = ADMIN_ROLE if reader_role == COMPANY_ROLE else COMPANY_ROLE
    marked = (
        db.query(SupportMessage)
        .filter(
            SupportMessage.company_id == company_id,
            SupportMessage.sender_role == other_side,
            SupportMessage.read_at.is_(None),
        )
        .update({SupportMessage.read_at: datetime.now(timezone.utc)}, synchronize_session=False)
    )
    db.commit()
    return marked or 0


def _store_message(db: Session, company: Company, sender: Company, body: str) -> SupportMessage:
    message = SupportMessage(
        company_id=company.id,
        sender_id=sender.id,
        sender_role=sender_role_of(sender),
        body=body,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


async def _publish(message: SupportMessage, company: Company, exclude: WebSocket | None = None) -> None:
    """
    Push a stored message to the live sockets: everyone watching this thread, and
    every Super Admin (for the inbox badge) when it came from a company.
    """
    payload = _to_response(message, company).model_dump(mode="json")
    await support_manager.broadcast_to_thread(company.id, {"type": "message", "message": payload}, exclude=exclude)

    if message.sender_role == COMPANY_ROLE:
        await support_manager.broadcast_to_admins(
            {
                "type": "inbox",
                "company_id": company.id,
                "company_name": company.name or company.email,
                "preview": message.body[:120],
                "created_at": payload.get("created_at"),
            },
            exclude=exclude,
        )


@router.get(
    "/messages",
    response_model=SupportThreadResponse,
    summary="The Support chat history for one company (oldest first)",
)
def get_thread(
    company_id: Optional[int] = Query(None, description="Required for a Super Admin: whose chat to open"),
    limit: int = Query(200, ge=1, le=500, description="How many of the most recent messages to return"),
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    company = _thread_company(db, current_user, company_id)

    recent = (
        db.query(SupportMessage)
        .filter(SupportMessage.company_id == company.id)
        .order_by(SupportMessage.id.desc())
        .limit(limit)
        .all()
    )
    messages = [_to_response(m, company) for m in reversed(recent)]

    my_side = sender_role_of(current_user)
    unread = sum(1 for m in messages if m.sender_role != my_side and m.read_at is None)

    return SupportThreadResponse(
        company_id=company.id,
        company_name=company.name or company.email,
        company_email=company.email,
        messages=messages,
        unread_count=unread,
    )


@router.post(
    "/messages",
    response_model=SupportMessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Send a Support chat message (HTTP fallback for when WebSockets are unavailable)",
)
async def send_message(
    payload: SendSupportMessageRequest,
    company_id: Optional[int] = Query(None, description="Required for a Super Admin: whose chat to reply in"),
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The message is empty.")

    company = _thread_company(db, current_user, company_id)
    message = _store_message(db, company, current_user, body)

    # Reaches anyone connected by WebSocket, so a polling client and a live one
    # can hold the same conversation.
    await _publish(message, company)

    return _to_response(message, company)


@router.get(
    "/conversations",
    response_model=SupportConversationsResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="Super Admin Support inbox: every company's thread with its last message and unread count",
)
def list_conversations(
    db: Session = Depends(deps.get_db),
) -> Any:
    latest_ids = [
        row[0]
        for row in db.query(func.max(SupportMessage.id)).group_by(SupportMessage.company_id).all()
        if row[0] is not None
    ]
    if not latest_ids:
        return SupportConversationsResponse(conversations=[], total_unread=0)

    last_messages = db.query(SupportMessage).filter(SupportMessage.id.in_(latest_ids)).all()

    unread_by_company: Dict[int, int] = {
        company_id: count
        for company_id, count in db.query(SupportMessage.company_id, func.count(SupportMessage.id))
        .filter(
            SupportMessage.sender_role == COMPANY_ROLE,
            SupportMessage.read_at.is_(None),
        )
        .group_by(SupportMessage.company_id)
        .all()
    }

    company_ids = [m.company_id for m in last_messages]
    companies = {
        company.id: company
        for company in db.query(Company).filter(Company.id.in_(company_ids)).all()
    }

    rows: List[SupportConversationRow] = []
    for message in last_messages:
        company = companies.get(message.company_id)
        if company is None:
            continue  # the company was deleted; its thread has nothing to show
        rows.append(
            SupportConversationRow(
                company_id=company.id,
                company_name=company.name or company.email,
                company_email=company.email,
                avatar_url=company.avatar_url,
                last_message=message.body[:200],
                last_message_at=message.created_at,
                last_sender_role=message.sender_role,
                unread_count=unread_by_company.get(company.id, 0),
            )
        )

    # Busiest first, so anyone waiting on a reply is at the top.
    rows.sort(key=lambda r: (r.unread_count > 0, r.last_message_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    return SupportConversationsResponse(conversations=rows, total_unread=sum(unread_by_company.values()))


@router.post(
    "/messages/read",
    response_model=MarkReadResponse,
    summary="Mark the other side's messages in this thread as read",
)
def mark_read(
    company_id: Optional[int] = Query(None, description="Required for a Super Admin: whose chat was read"),
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    company = _thread_company(db, current_user, company_id)
    marked = _mark_thread_read(db, company.id, sender_role_of(current_user))
    return MarkReadResponse(marked_read=marked)


@router.get(
    "/unread-count",
    response_model=UnreadCountResponse,
    summary="Unread total for the badge on the Support button",
)
def unread_count(
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    return UnreadCountResponse(unread_count=_unread_for(db, current_user))


def _user_from_token(token: str, db: Session) -> Optional[Company]:
    """
    Authenticate a WebSocket handshake. Browsers can't set headers on a WebSocket,
    so the access token arrives as a query parameter instead of a Bearer header.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None

    # A password-reset token also carries an email in `sub`; it must not open a chat.
    if payload.get("purpose"):
        return None

    email = payload.get("sub")
    if not email:
        return None

    user = db.query(Company).filter(Company.email == email).first()
    if user is None or not user.is_active:
        return None
    return user


@router.websocket("/ws")
async def support_socket(
    websocket: WebSocket,
    token: str = Query(..., description="The same JWT access token used for the REST API"),
    company_id: Optional[int] = Query(None, description="Required for a Super Admin: whose chat to join"),
) -> None:
    """
    Live Support chat.

    Send `{"type": "message", "body": "..."}` to post, `{"type": "read"}` to clear
    the unread badge on the other side, and `{"type": "ping"}` to keep the socket
    warm. The server sends `{"type": "ready"}` once on connect, then
    `{"type": "message"}`, `{"type": "read"}` and - for Super Admins - an
    `{"type": "inbox"}` nudge for threads they are not currently watching.

    Note: this needs a long-running server. On Vercel's serverless functions the
    handshake cannot succeed, so the client falls back to polling the REST
    endpoints above and the chat keeps working with a short delay.
    """
    db = SessionLocal()
    try:
        user = _user_from_token(token, db)
        if user is None:
            await websocket.close(code=4401)
            return

        admin = is_admin(user)
        my_role = ADMIN_ROLE if admin else COMPANY_ROLE

        if admin:
            if company_id is None:
                await websocket.close(code=4400)
                return
            target = db.query(Company).filter(Company.id == company_id).first()
            if target is None or target.role == Role.SUPERADMIN:
                await websocket.close(code=4404)
                return
        else:
            if company_id is not None and company_id != user.id:
                await websocket.close(code=4403)
                return
            target = user

        thread_id = target.id
        thread_name = target.name or target.email
        user_id = user.id
    finally:
        db.close()

    await websocket.accept()
    await support_manager.join_thread(thread_id, websocket)
    if admin:
        await support_manager.join_admins(websocket)

    await websocket.send_json(
        {
            "type": "ready",
            "company_id": thread_id,
            "company_name": thread_name,
            "role": my_role,
        }
    )

    try:
        while True:
            try:
                incoming = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:  # noqa: BLE001 - anything that isn't JSON
                await websocket.send_json({"type": "error", "detail": "Send JSON, for example {\"type\":\"message\",\"body\":\"hi\"}."})
                continue

            kind = (incoming or {}).get("type")

            if kind == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if kind == "message":
                body = str((incoming or {}).get("body") or "").strip()
                if not body:
                    await websocket.send_json({"type": "error", "detail": "The message is empty."})
                    continue
                if len(body) > MAX_BODY_LENGTH:
                    await websocket.send_json(
                        {"type": "error", "detail": f"A message can be at most {MAX_BODY_LENGTH} characters."}
                    )
                    continue

                session = SessionLocal()
                try:
                    company = session.query(Company).filter(Company.id == thread_id).first()
                    sender = session.query(Company).filter(Company.id == user_id).first()
                    if company is None or sender is None:
                        await websocket.send_json({"type": "error", "detail": "This chat is no longer available."})
                        continue
                    stored = _store_message(session, company, sender, body)
                    payload = _to_response(stored, company).model_dump(mode="json")
                finally:
                    session.close()

                # The sender gets it back too, so both sides render from the saved row.
                await websocket.send_json({"type": "message", "message": payload})
                await support_manager.broadcast_to_thread(
                    thread_id, {"type": "message", "message": payload}, exclude=websocket
                )
                if my_role == COMPANY_ROLE:
                    await support_manager.broadcast_to_admins(
                        {
                            "type": "inbox",
                            "company_id": thread_id,
                            "company_name": thread_name,
                            "preview": body[:120],
                            "created_at": payload.get("created_at"),
                        },
                        exclude=websocket,
                    )
                continue

            if kind == "read":
                session = SessionLocal()
                try:
                    marked = _mark_thread_read(session, thread_id, my_role)
                finally:
                    session.close()
                await websocket.send_json({"type": "read", "marked_read": marked})
                if marked:
                    await support_manager.broadcast_to_thread(
                        thread_id,
                        {"type": "read", "company_id": thread_id, "read_by": my_role},
                        exclude=websocket,
                    )
                continue

            await websocket.send_json({"type": "error", "detail": f"Unknown message type '{kind}'."})

    except WebSocketDisconnect:
        pass
    except Exception as err:  # noqa: BLE001 - never let one socket take the worker down
        import logging

        logging.getLogger("SupportWebSocket").info("Support socket ended unexpectedly: %s", err)
    finally:
        await support_manager.leave_thread(thread_id, websocket)
        if admin:
            await support_manager.leave_admins(websocket)
