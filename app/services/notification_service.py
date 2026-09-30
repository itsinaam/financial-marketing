import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption import decrypt, encrypt
from app.models.notification import NotificationChannel
from app.services.notification_oauth_service import (
    NotificationOAuthError,
    graph_post,
    refresh_microsoft_token,
)

logger = logging.getLogger("NotificationService")

PROVIDERS = ("whatsapp", "slack", "teams")
EVENTS = ("ready_for_approval", "published", "failed")

SEND_TIMEOUT_SECONDS = 8

# Webhooks are POSTed from our server, so only the providers' own hosts are allowed;
# otherwise an account could point one at an internal address.
_SLACK_HOSTS = ("hooks.slack.com",)
_TEAMS_HOST_SUFFIXES = (
    ".logic.azure.com",
    ".webhook.office.com",
    ".powerplatform.com",
    ".powerautomate.com",
)

_PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")

WHATSAPP_NOT_CONFIGURED = (
    "WhatsApp sending isn't set up on the server yet. Add WHATSAPP_PHONE_NUMBER_ID and "
    "WHATSAPP_ACCESS_TOKEN to the environment."
)


class NotificationError(Exception):
    """A channel could not be delivered to, with a message safe to show the user."""


def validate_webhook_url(provider: str, url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise NotificationError("The webhook URL must be a full https:// address.")

    if provider == "slack" and host not in _SLACK_HOSTS:
        raise NotificationError("That isn't a Slack webhook. It should start with https://hooks.slack.com/.")
    if provider == "teams" and not host.endswith(_TEAMS_HOST_SUFFIXES):
        raise NotificationError(
            "That isn't a Microsoft Teams webhook. Create one with the Workflows app in the "
            "channel ('Post to a channel when a webhook request is received')."
        )
    return cleaned


def validate_whatsapp_number(number: str) -> str:
    """WhatsApp is messaged per phone number, so the target is a number in international form."""
    cleaned = re.sub(r"[\s()\-]", "", number.strip())
    if not _PHONE_RE.match(cleaned):
        raise NotificationError(
            "Enter the WhatsApp number in international form, such as +923001234567."
        )
    return cleaned if cleaned.startswith("+") else f"+{cleaned}"


def whatsapp_is_configured() -> bool:
    return bool(settings.WHATSAPP_PHONE_NUMBER_ID and settings.WHATSAPP_ACCESS_TOKEN)


def send_whatsapp(number: str, message: str) -> None:
    """Send a plain text WhatsApp message through the Meta Cloud API."""
    if not whatsapp_is_configured():
        raise NotificationError(WHATSAPP_NOT_CONFIGURED)

    endpoint = (
        f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/"
        f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    try:
        res = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": number.lstrip("+"),
                "type": "text",
                "text": {"body": message},
            },
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except requests.RequestException as err:
        raise NotificationError(f"Couldn't reach WhatsApp: {err}") from err

    if 200 <= res.status_code < 300:
        return

    detail = ""
    try:
        error = res.json().get("error", {})
        detail = error.get("message", "")
        # Outside the 24 hour window WhatsApp only allows approved templates.
        if error.get("code") == 131047:
            raise NotificationError(
                "WhatsApp only allows a plain message within 24 hours of the recipient "
                "messaging your business number. Ask them to send it a message first, or "
                "use an approved template."
            )
    except ValueError:
        detail = res.text[:200]
    raise NotificationError(f"WhatsApp rejected the message ({res.status_code}): {detail}")


def _payload(provider: str, message: str) -> dict:
    if provider == "slack":
        return {"text": message}
    # Teams Workflows webhooks take an Adaptive Card wrapped in a message.
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": message, "wrap": True}],
                },
            }
        ],
    }


def get_teams_access_token(channel: NotificationChannel) -> str:
    if not channel.access_token:
        raise NotificationError("Teams isn't connected. Connect it again from Notifications settings.")

    try:
        expires_at = channel.token_expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at <= datetime.now(timezone.utc):
            if not channel.refresh_token:
                raise NotificationError("Teams authorization expired. Connect Teams again.")
            refreshed = refresh_microsoft_token(decrypt(channel.refresh_token))
            channel.access_token = encrypt(refreshed["access_token"])
            channel.refresh_token = encrypt(refreshed.get("refresh_token") or decrypt(channel.refresh_token))
            expires_in = int(refreshed.get("expires_in", 3600))
            channel.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        return decrypt(channel.access_token)
    except NotificationOAuthError as err:
        raise NotificationError(f"Teams authorization expired. Reconnect Teams: {err}") from err
    except RuntimeError as err:
        raise NotificationError(str(err)) from err


def send_to_channel(channel: NotificationChannel, message: str) -> None:
    """Deliver one message, raising NotificationError with a user-facing reason on failure."""
    if channel.provider == "whatsapp":
        if not channel.target:
            raise NotificationError("No WhatsApp number saved yet.")
        send_whatsapp(channel.target, message)
        return
    if channel.provider == "teams" and not channel.webhook_url:
        if not channel.team_id or not channel.channel_id:
            raise NotificationError("Choose a Teams channel to finish connecting Teams.")
        access_token = get_teams_access_token(channel)
        try:
            graph_post(
                access_token,
                f"/teams/{channel.team_id}/channels/{channel.channel_id}/messages",
                {"body": {"contentType": "text", "content": message}},
            )
        except NotificationOAuthError as err:
            raise NotificationError(str(err)) from err
        return
    if not channel.webhook_url:
        raise NotificationError(f"{channel.provider.title()} isn't connected yet.")

    try:
        res = requests.post(
            channel.webhook_url,
            json=_payload(channel.provider, message),
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except requests.RequestException as err:
        raise NotificationError(f"Couldn't reach {channel.provider.title()}: {err}") from err

    if not 200 <= res.status_code < 300:
        raise NotificationError(
            f"{channel.provider.title()} rejected the message ({res.status_code}): {res.text[:200]}"
        )


def notify(db: Session, company_id: int, event: str, message: str) -> None:
    """
    Alert every channel the company has connected and switched on for this event.
    Never raises: a notification problem must not break generating or publishing.
    """
    try:
        column = {
            "ready_for_approval": NotificationChannel.notify_ready_for_approval,
            "published": NotificationChannel.notify_published,
            "failed": NotificationChannel.notify_failed,
        }[event]
        channels = [
            channel
            for channel in db.query(NotificationChannel)
            .filter(NotificationChannel.company_id == company_id, column == True)  # noqa: E712
            .all()
            if (channel.target if channel.provider == "whatsapp" else channel.webhook_url)
        ]
    except Exception as err:
        logger.warning("Could not load notification channels for company %s: %s", company_id, err)
        return

    for channel in channels:
        previous_access_token = channel.access_token
        try:
            send_to_channel(channel, message)
        except Exception as err:
            logger.warning("Notification to %s for company %s failed: %s", channel.provider, company_id, err)
        finally:
            if channel.access_token != previous_access_token:
                db.commit()


def titles_summary(titles: list[str], limit: int = 3) -> str:
    shown = [t for t in titles if t][:limit]
    extra = len(titles) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {extra} more" if extra > 0 else text
