from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse
from app.core import deps
from app.api.credentials import get_request_base_url, resolve_company
from app.core.config import settings
from app.core.encryption import encrypt
from app.models.notification import NotificationChannel
from app.schemas.notifications import (
    NotificationChannelList,
    NotificationChannelResponse,
    NotificationTriggers,
    SaveChannelRequest,
    TeamsChannelSelection,
    TestChannelResponse,
)
from app.services.notification_service import (
    PROVIDERS,
    NotificationError,
    send_to_channel,
    get_teams_access_token,
    validate_webhook_url,
    validate_whatsapp_number,
    whatsapp_is_configured,
)
from app.services.notification_oauth_service import (
    NotificationOAuthError,
    create_state,
    exchange_microsoft_code,
    exchange_slack_code,
    graph_get,
    microsoft_authorization_url,
    read_state,
    slack_authorization_url,
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
    if channel.provider == "teams":
        return bool(channel.webhook_url or (channel.access_token and channel.channel_id))
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


def _oauth_redirect_uri(request: Request, provider: str) -> str:
    configured_uri = (
        settings.SLACK_REDIRECT_URI if provider == "slack" else settings.MICROSOFT_TEAMS_REDIRECT_URI
    )
    return configured_uri.strip() or (
        f"{get_request_base_url(request)}"
        f"{settings.API_V1_STR}/notifications/oauth/{provider}/callback"
    )


def _channel_for_oauth(db: Session, company_id: int, provider: str) -> NotificationChannel:
    channel = _get_channel(db, company_id, provider)
    if channel is None:
        channel = NotificationChannel(company_id=company_id, provider=provider)
        db.add(channel)
    return channel


def _save_microsoft_token(channel: NotificationChannel, token_data: dict) -> None:
    channel.access_token = encrypt(token_data["access_token"])
    if token_data.get("refresh_token"):
        channel.refresh_token = encrypt(token_data["refresh_token"])
    channel.token_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_data.get("expires_in", 3600))
    )


@router.get("/oauth/slack/connect", summary="Start Slack OAuth connection")
def connect_slack(
    request: Request,
    company_id: Optional[int] = Query(None),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> dict:
    company = resolve_company(db, auth, company_id)
    if not settings.SLACK_CLIENT_ID or not settings.SLACK_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET on the server first.",
        )
    redirect_uri = _oauth_redirect_uri(request, "slack")
    state = create_state("slack", company.id)
    return {
        "provider": "slack",
        "authorization_url": slack_authorization_url(settings.SLACK_CLIENT_ID, redirect_uri, state),
        "redirect_uri": redirect_uri,
    }


@router.get("/oauth/slack/callback", summary="Finish Slack OAuth connection")
def slack_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(deps.get_db),
) -> dict:
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Slack authorization failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Slack did not return an authorization code and state.")
    try:
        company_id = read_state(state, "slack")
        data = exchange_slack_code(code, _oauth_redirect_uri(request, "slack"))
    except NotificationOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    webhook = data.get("incoming_webhook") or {}
    if not webhook.get("url"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Slack did not return an incoming webhook.")
    channel = _channel_for_oauth(db, company_id, "slack")
    channel.webhook_url = webhook["url"]
    channel.target = webhook.get("channel") or webhook.get("channel_id")
    channel.last_error = None
    db.commit()
    return RedirectResponse(
        url="https://financial-markett.vercel.app/notifications",
        status_code=302,
    )


@router.get("/oauth/teams/connect", summary="Start Microsoft Teams OAuth connection")
def connect_teams(
    request: Request,
    company_id: Optional[int] = Query(None),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> dict:
    company = resolve_company(db, auth, company_id)
    if not settings.MICROSOFT_CLIENT_ID or not settings.MICROSOFT_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Set MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET on the server first.",
        )
    if not settings.CREDENTIALS_ENCRYPTION_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Set CREDENTIALS_ENCRYPTION_KEY before connecting Microsoft Teams.",
        )
    redirect_uri = _oauth_redirect_uri(request, "teams")
    state = create_state("teams", company.id)
    return {
        "provider": "teams",
        "authorization_url": microsoft_authorization_url(settings.MICROSOFT_CLIENT_ID, redirect_uri, state),
        "redirect_uri": redirect_uri,
    }


@router.get("/oauth/teams/callback", summary="Finish Microsoft Teams OAuth connection")
def teams_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(deps.get_db),
) -> dict:
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Microsoft authorization failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Microsoft did not return an authorization code and state.")
    try:
        company_id = read_state(state, "teams")
        token_data = exchange_microsoft_code(code, _oauth_redirect_uri(request, "teams"))
        channel = _channel_for_oauth(db, company_id, "teams")
        _save_microsoft_token(channel, token_data)
    except NotificationOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    channel.webhook_url = None
    channel.team_id = None
    channel.channel_id = None
    channel.target = None
    channel.last_error = None
    db.commit()
    return {"status": "success", "provider": "teams", "next_step": "Select a team and channel from the Teams connection settings."}


@router.get("/teams/teams", summary="List Teams available to the connected Microsoft account")
def list_teams(
    company_id: Optional[int] = Query(None),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> dict:
    company = resolve_company(db, auth, company_id)
    channel = _get_channel(db, company.id, "teams")
    if channel is None or not channel.access_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Connect Microsoft Teams first.")
    try:
        token = get_teams_access_token(channel)
        data = graph_get(token, "/me/joinedTeams?$select=id,displayName")
    except (NotificationError, NotificationOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if db.is_modified(channel):
        db.commit()
    return {"teams": [{"id": item["id"], "name": item["displayName"]} for item in data.get("value", [])]}


@router.get("/teams/teams/{team_id}/channels", summary="List channels in a joined Team")
def list_team_channels(
    team_id: str,
    company_id: Optional[int] = Query(None),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> dict:
    company = resolve_company(db, auth, company_id)
    channel = _get_channel(db, company.id, "teams")
    if channel is None or not channel.access_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Connect Microsoft Teams first.")
    try:
        token = get_teams_access_token(channel)
        data = graph_get(token, f"/teams/{quote(team_id, safe='')}/channels?$select=id,displayName")
    except (NotificationError, NotificationOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if db.is_modified(channel):
        db.commit()
    return {"channels": [{"id": item["id"], "name": item["displayName"]} for item in data.get("value", [])]}


@router.put("/teams/channel", summary="Save the Teams channel chosen by the user")
def select_teams_channel(
    payload: TeamsChannelSelection,
    company_id: Optional[int] = Query(None),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> NotificationChannelResponse:
    company = resolve_company(db, auth, company_id)
    channel = _get_channel(db, company.id, "teams")
    if channel is None or not channel.access_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Connect Microsoft Teams first.")
    try:
        token = get_teams_access_token(channel)
        data = graph_get(
            token,
            f"/teams/{quote(payload.team_id, safe='')}/channels?$select=id,displayName",
        )
    except (NotificationError, NotificationOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    selected = next((item for item in data.get("value", []) if item.get("id") == payload.channel_id), None)
    if selected is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That channel was not found in the selected Team.")

    channel.team_id = payload.team_id
    channel.channel_id = payload.channel_id
    channel.target = selected.get("displayName")
    channel.last_error = None
    db.commit()
    db.refresh(channel)
    return _to_response("teams", channel)


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

    db.delete(channel)
    db.commit()
    return _to_response(name, None)


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
