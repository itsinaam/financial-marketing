import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.credentials import Credentials
from app.models.post import GeneratedPost
from app.services.post_generator_service import publish_post_to_platform

logger = logging.getLogger("SchedulerService")

# Naive date/time strings (no timezone info) are interpreted in this local offset
# before being compared against the current UTC time.
APP_TIMEZONE_OFFSET_HOURS = int(os.getenv("APP_TIMEZONE_OFFSET_HOURS", "5"))
LOCAL_TZ = timezone(timedelta(hours=APP_TIMEZONE_OFFSET_HOURS))

CHECK_INTERVAL_SECONDS = 30

_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %I:%M %p",
]


def parse_scheduled_datetime(date_str: str | None, time_str: str | None) -> datetime | None:
    """
    Combine the post's `date` and `start_time` strings into a UTC-aware datetime.
    Returns None if either field is missing or the combination can't be parsed
    (a post with no valid schedule is left for manual publishing only).
    """
    if not date_str or not time_str:
        return None

    combined = f"{date_str.strip()} {time_str.strip()}"
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(combined, fmt)
            return dt.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(combined.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(timezone.utc)
    except ValueError:
        logger.warning("Could not parse scheduled datetime from date=%r start_time=%r", date_str, time_str)
        return None


def check_and_publish_due_posts(db: Session) -> list[dict]:
    """
    Publish every approved, not-yet-posted GeneratedPost whose scheduled date/time
    has arrived. Posts without a valid schedule are skipped (manual publish only).
    """
    results = []
    now_utc = datetime.now(timezone.utc)

    due_candidates = (
        db.query(GeneratedPost)
        .filter(
            GeneratedPost.is_approved == True,  # noqa: E712
            GeneratedPost.is_posted == False,  # noqa: E712
            GeneratedPost.date.is_not(None),
            GeneratedPost.start_time.is_not(None),
        )
        .all()
    )

    for post in due_candidates:
        scheduled_at = parse_scheduled_datetime(post.date, post.start_time)
        if not scheduled_at or scheduled_at > now_utc:
            continue

        # Mark as posted before the network call to avoid double-publishing if
        # two scheduler ticks overlap.
        post.is_posted = True
        db.commit()

        credential = (
            db.query(Credentials)
            .filter(Credentials.company_id == post.company_id, Credentials.platform == post.platform)
            .first()
        )

        try:
            if not credential:
                raise RuntimeError(f"No credentials found for platform '{post.platform}'.")
            detail = publish_post_to_platform(post, credential)
            post.posted_at = now_utc
            post.post_error = None
            db.commit()
            logger.info("Scheduled post '%s' published successfully to %s.", post.id, post.platform)
            results.append({"post_id": post.id, "platform": post.platform, "status": "success", "details": detail})
        except Exception as err:
            post.is_posted = False
            post.post_error = str(err)
            db.commit()
            logger.error("Scheduled post '%s' failed to publish: %s", post.id, err)
            results.append({"post_id": post.id, "platform": post.platform, "status": "failed", "error": str(err)})

    return results


async def scheduled_post_checker_loop() -> None:
    """Background task: periodically publish any due, approved posts."""
    logger.info("Scheduled post checker loop started (interval=%ss).", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            db = SessionLocal()
            try:
                check_and_publish_due_posts(db)
            finally:
                db.close()
        except asyncio.CancelledError:
            logger.info("Scheduled post checker loop cancelled.")
            break
        except Exception as err:
            logger.error("Error in scheduled post checker loop: %s", err)
