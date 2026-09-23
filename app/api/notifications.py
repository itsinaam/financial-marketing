from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.notification import NotificationChannel
from app.schemas.notifications import (
    NotificationChannelList,
    NotificationChannelResponse,
    NotificationTriggers,
    SaveChannelRequest,
    TestChannelResponse,
)
from app.services.notification_service import (
    PROVIDERS,
    NotificationError,
    send_to_channel,
    validate_webhook_url,
    validate_whatsapp_number,
    whatsapp_is_configured,
)

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

TEST_MESSAGE = "This is a test message from Financial Market. Notifications are set up correctly."


def _check_provider(provider: str) -> str:
    name = provider.strip().lower()
    if name not in PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown provider '{provider}'. Use one of: {', '.join(PROVIDERS)}.",
        )
    return name


def _is_connected(channel: NotificationChannel) -> bool:
    if channel.provider == "whatsapp":
        return bool(channel.target)
    return bool(channel.webhook_url)


def _can_send(provider: str) -> bool:
    """WhatsApp needs a sender configured on the server; the others only need a webhook."""
    return whatsapp_is_configured() if provider == "whatsapp" else True


def _to_response(provider: str, channel: Optional[NotificationChannel]) -> NotificationChannelResponse:
    if channel is None:
        return NotificationChannelResponse(
            provider=provider,
            is_connected=False,
            webhook_configured=False,
            can_send=_can_send(provider),
            triggers=NotificationTriggers(ready_for_approval=False, published=False, failed=False),
        )
    return NotificationChannelResponse(
        provider=provider,
        is_connected=_is_connected(channel),
        target=channel.target,
        webhook_configured=bool(channel.webhook_url),
        can_send=_can_send(provider),
        triggers=NotificationTriggers(
            ready_for_approval=channel.notify_ready_for_approval,
            published=channel.notify_published,
            failed=channel.notify_failed,
        ),
        last_error=channel.last_error,
        updated_at=channel.updated_at,
    )


def _get_channel(db: Session, company_id: int, provider: str) -> Optional[NotificationChannel]:
    return (
        db.query(NotificationChannel)
        .filter(NotificationChannel.company_id == company_id, NotificationChannel.provider == provider)
        .first()
    )


@router.get(
    "/channels",
    response_model=NotificationChannelList,
    summary="WhatsApp, Slack and Teams: connection status and triggers",
)
def list_channels(
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """All three providers are always returned, so the cards render even before anything is connected."""
    company = resolve_company(db, auth, company_id)
    return NotificationChannelList(
        channels=[_to_response(provider, _get_channel(db, company.id, provider)) for provider in PROVIDERS]
    )


@router.put(
    "/channels/{provider}",
    response_model=NotificationChannelResponse,
    summary="Connect or configure a channel, and switch its triggers on or off",
)
def save_channel(
    provider: str,
    payload: SaveChannelRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Every field is optional, so the same call serves Connect, Configure and a single
    trigger toggle. Slack and Teams connect with a webhook_url; WhatsApp with an invite link
    in target.
    """
    name = _check_provider(provider)
    company = resolve_company(db, auth, company_id)

    try:
        if name == "whatsapp" and payload.target is not None:
            payload.target = validate_whatsapp_number(payload.target)
        webhook = None
        if payload.webhook_url is not None:
            if name == "whatsapp":
                raise NotificationError("WhatsApp connects with a phone number in target, not a webhook.")
            webhook = validate_webhook_url(name, payload.webhook_url)
    except NotificationError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))

    channel = _get_channel(db, company.id, name)
    if channel is None:
        channel = NotificationChannel(company_id=company.id, provider=name)
        db.add(channel)

    if payload.target is not None:
        channel.target = payload.target.strip() or None
    if webhook is not None:
        channel.webhook_url = webhook
        channel.last_error = None
    if payload.ready_for_approval is not None:
        channel.notify_ready_for_approval = payload.ready_for_approval
    if payload.published is not None:
        channel.notify_published = payload.published
    if payload.failed is not None:
        channel.notify_failed = payload.failed

    db.commit()
    db.refresh(channel)
    return _to_response(name, channel)


@router.delete(
    "/channels/{provider}",
    response_model=NotificationChannelResponse,
    summary="Disconnect a channel (its trigger choices are kept)",
)
def disconnect_channel(
    provider: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    name = _check_provider(provider)
    company = resolve_company(db, auth, company_id)
    channel = _get_channel(db, company.id, name)
    if channel is None:
        return _to_response(name, None)

    channel.webhook_url = None
    if name == "whatsapp":
        channel.target = None
    channel.last_error = None
    db.commit()
    db.refresh(channel)
    return _to_response(name, channel)


@router.post(
    "/channels/{provider}/test",
    response_model=TestChannelResponse,
    summary="Send a test message (the Test Connection button)",
)
def test_channel(
    provider: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    name = _check_provider(provider)
    company = resolve_company(db, auth, company_id)
    channel = _get_channel(db, company.id, name)
    if channel is None or not _is_connected(channel):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name.title()} isn't connected yet.",
        )

    try:
        send_to_channel(channel, TEST_MESSAGE)
    except NotificationError as err:
        channel.last_error = str(err)
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))

    channel.last_error = None
    db.commit()
    return TestChannelResponse(success=True, message=f"Test message sent to {name.title()}.")
