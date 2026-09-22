import logging
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel

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

WHATSAPP_UNSUPPORTED = (
    "WhatsApp groups can't be messaged through their invite link: the official WhatsApp "
    "API only sends to individual phone numbers. The settings are saved, but no alerts "
    "are delivered to WhatsApp yet."
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


def validate_whatsapp_link(link: str) -> str:
    cleaned = link.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "chat.whatsapp.com" or len(parsed.path) <= 1:
        raise NotificationError("The invite link should look like https://chat.whatsapp.com/XXXX.")
    return cleaned


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


def send_to_channel(channel: NotificationChannel, message: str) -> None:
    """Deliver one message, raising NotificationError with a user-facing reason on failure."""
    if channel.provider == "whatsapp":
        raise NotificationError(WHATSAPP_UNSUPPORTED)
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
        channels = (
            db.query(NotificationChannel)
            .filter(
                NotificationChannel.company_id == company_id,
                NotificationChannel.provider != "whatsapp",
                NotificationChannel.webhook_url.is_not(None),
                column == True,  # noqa: E712
            )
            .all()
        )
    except Exception as err:
        logger.warning("Could not load notification channels for company %s: %s", company_id, err)
        return

    for channel in channels:
        try:
            send_to_channel(channel, message)
        except Exception as err:
            logger.warning("Notification to %s for company %s failed: %s", channel.provider, company_id, err)


def titles_summary(titles: list[str], limit: int = 3) -> str:
    shown = [t for t in titles if t][:limit]
    extra = len(titles) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {extra} more" if extra > 0 else text
