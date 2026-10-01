import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.core.config import settings

REFERRAL_REWARD_CREDITS = 100


class ReferralServiceError(Exception):
    pass


def build_referral_link(referrer_id: int) -> str:
    signup_url = settings.REFERRAL_SIGNUP_URL.strip()
    parsed = urlparse(signup_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ReferralServiceError(
            "Referral signup isn't configured. Set REFERRAL_SIGNUP_URL to the frontend signup page."
        )

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["referrer_id"] = str(referrer_id)
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_password_reset_link(token: str) -> str:
    reset_url = settings.PASSWORD_RESET_URL.strip() or (
        f"{settings.PUBLIC_BASE_URL.rstrip('/')}/reset-password"
    )
    parsed = urlparse(reset_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ReferralServiceError(
            "Password reset isn't configured. Set PASSWORD_RESET_URL or PUBLIC_BASE_URL."
        )

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["token"] = token
    return urlunparse(parsed._replace(query=urlencode(query)))


def _send_email(recipient: str, subject: str, body: str, reply_to: str | None = None) -> None:
    if not all((settings.SMTP_HOST, settings.SMTP_USER, settings.SMTP_PASSWORD)):
        raise ReferralServiceError(
            "Email sending isn't configured. Set SMTP_HOST, SMTP_PORT, SMTP_USER, and SMTP_PASSWORD."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.EMAILS_FROM_NAME, settings.SMTP_USER))
    message["To"] = recipient
    # Everything here is sent from one mailbox, so without this a reply would go
    # back to that mailbox instead of the person being answered.
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)

    try:
        context = ssl.create_default_context()
        if settings.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, context=context, timeout=15
            ) as smtp:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise ReferralServiceError("The email could not be sent. Check the SMTP settings.") from exc


def send_referral_email(recipient: str, referrer_name: str, referral_link: str) -> None:
    _send_email(
        recipient,
        f"{referrer_name} invited you to join {settings.APP_NAME}",
        f"{referrer_name} invited you to join {settings.APP_NAME}.\n\n"
        f"Create your account here: {referral_link}\n",
    )


def send_password_reset_email(recipient: str, reset_link: str) -> None:
    _send_email(
        recipient,
        f"Reset your {settings.APP_NAME} password",
        f"Use this link to reset your password (it expires in 30 minutes):\n\n"
        f"{reset_link}\n\nIf you did not request a password reset, ignore this email.",
    )
