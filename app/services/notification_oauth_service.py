from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import secrets

import requests
from jose import JWTError, jwt

from app.core.config import settings

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
MICROSOFT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MICROSOFT_SCOPES = "openid profile offline_access User.Read Team.ReadBasic.All Channel.ReadBasic.All ChannelMessage.Send"


class NotificationOAuthError(Exception):
    """An OAuth provider rejected or could not complete a notification connection."""


def create_state(provider: str, company_id: int) -> str:
    return jwt.encode(
        {
            "provider": provider,
            "company_id": company_id,
            "nonce": secrets.token_urlsafe(24),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def read_state(state: str, provider: str) -> int:
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        company_id = payload.get("company_id")
        if payload.get("provider") != provider or not payload.get("nonce") or not company_id:
            raise NotificationOAuthError("Invalid OAuth state. Start the connection again.")
        return int(company_id)
    except (JWTError, TypeError, ValueError) as exc:
        raise NotificationOAuthError("Invalid or expired OAuth state. Start the connection again.") from exc


def slack_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return f"{SLACK_AUTHORIZE_URL}?{urlencode({'client_id': client_id, 'scope': 'incoming-webhook', 'redirect_uri': redirect_uri, 'state': state})}"


def exchange_slack_code(code: str, redirect_uri: str) -> dict:
    try:
        response = requests.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": settings.SLACK_CLIENT_ID,
                "client_secret": settings.SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=20,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise NotificationOAuthError("Could not exchange the Slack authorization code.") from exc
    if response.status_code != 200 or not data.get("ok"):
        raise NotificationOAuthError(f"Slack OAuth failed: {data.get('error', 'unknown_error')}")
    return data


def microsoft_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    tenant = settings.MICROSOFT_TENANT_ID.strip() or "organizations"
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": MICROSOFT_SCOPES,
        "state": state,
    }
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{urlencode(params)}"


def _microsoft_token_request(payload: dict) -> dict:
    tenant = settings.MICROSOFT_TENANT_ID.strip() or "organizations"
    try:
        response = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data=payload,
            timeout=20,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise NotificationOAuthError("Could not reach Microsoft OAuth.") from exc
    if response.status_code != 200:
        description = data.get("error_description", "Token request was rejected.")
        raise NotificationOAuthError(f"Microsoft OAuth failed: {description[:300]}")
    return data


def exchange_microsoft_code(code: str, redirect_uri: str) -> dict:
    return _microsoft_token_request(
        {
            "client_id": settings.MICROSOFT_CLIENT_ID,
            "client_secret": settings.MICROSOFT_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": MICROSOFT_SCOPES,
        }
    )


def refresh_microsoft_token(refresh_token: str) -> dict:
    return _microsoft_token_request(
        {
            "client_id": settings.MICROSOFT_CLIENT_ID,
            "client_secret": settings.MICROSOFT_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": MICROSOFT_SCOPES,
        }
    )


def graph_get(access_token: str, path: str) -> dict:
    try:
        response = requests.get(
            f"{MICROSOFT_GRAPH_BASE}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise NotificationOAuthError("Could not reach Microsoft Graph.") from exc
    if response.status_code != 200:
        raise NotificationOAuthError(
            f"Microsoft Graph rejected the request ({response.status_code}): {response.text[:250]}"
        )
    return response.json()


def graph_post(access_token: str, path: str, payload: dict) -> dict:
    try:
        response = requests.post(
            f"{MICROSOFT_GRAPH_BASE}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise NotificationOAuthError("Could not reach Microsoft Graph.") from exc
    if response.status_code not in (200, 201):
        raise NotificationOAuthError(
            f"Microsoft Graph rejected the message ({response.status_code}): {response.text[:250]}"
        )
    return response.json() if response.content else {}