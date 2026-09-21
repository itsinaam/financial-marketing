import re
from datetime import datetime, timezone
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.post import GeneratedPost
from app.models.blog import GeneratedBlog
from app.schemas.approvals import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalQueueItem,
    ApprovalQueueResponse,
)

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

# Drafts scoring below this are surfaced as "Flagged" so a reviewer looks at them
# before anything goes out.
DEFAULT_FLAG_THRESHOLD = 50

_TAG_RE = re.compile(r"<[^>]+>")
PREVIEW_LENGTH = 200


def _preview_text(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"\s{2,}", " ", _TAG_RE.sub(" ", raw)).strip()
    return text[:PREVIEW_LENGTH] if text else None


def _is_flagged(score: int | None, threshold: int) -> bool:
    return score is not None and score < threshold


@router.get(
    "",
    response_model=ApprovalQueueResponse,
    summary="List posts and blogs waiting for review, with ready/flagged counts",
)
def get_approval_queue(
    content_type: Optional[str] = Query(None, description="Limit to 'post' or 'blog'; both are returned by default"),
    flagged_only: bool = Query(False, description="Return only drafts flagged by their AI safety score"),
    flag_threshold: int = Query(DEFAULT_FLAG_THRESHOLD, ge=0, le=100, description="AI safety score below which a draft is flagged"),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    The unified review queue behind the Approval Queue screen: every generated post
    and blog that is still unapproved and unpublished, newest first.
    """
    wanted = (content_type or "").strip().lower()
    if wanted and wanted not in {"post", "blog"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="content_type must be either 'post' or 'blog'.",
        )

    company = resolve_company(db, auth, company_id)
    items: List[ApprovalQueueItem] = []

    if wanted in ("", "post"):
        posts = (
            db.query(GeneratedPost)
            .filter(
                GeneratedPost.company_id == company.id,
                GeneratedPost.is_approved == False,  # noqa: E712
                GeneratedPost.is_posted == False,  # noqa: E712
            )
            .all()
        )
        for post in posts:
            items.append(
                ApprovalQueueItem(
                    id=post.id,
                    content_type="post",
                    title=post.title or post.headline,
                    preview=_preview_text(post.caption),
                    image_url=post.image_url,
                    platform=post.platform,
                    ai_safety_score=post.ai_safety_score,
                    language=post.language,
                    is_flagged=_is_flagged(post.ai_safety_score, flag_threshold),
                    date=post.date,
                    start_time=post.start_time,
                    created_at=post.created_at,
                )
            )

    if wanted in ("", "blog"):
        blogs = (
            db.query(GeneratedBlog)
            .filter(
                GeneratedBlog.company_id == company.id,
                GeneratedBlog.is_approved == False,  # noqa: E712
                GeneratedBlog.is_posted == False,  # noqa: E712
            )
            .all()
        )
        for blog in blogs:
            items.append(
                ApprovalQueueItem(
                    id=blog.id,
                    content_type="blog",
                    title=blog.title,
                    preview=_preview_text(blog.content),
                    image_url=blog.image_url,
                    platform=blog.platform,
                    ai_safety_score=blog.ai_safety_score,
                    language=blog.language,
                    is_flagged=_is_flagged(blog.ai_safety_score, flag_threshold),
                    date=blog.date,
                    start_time=blog.start_time,
                    created_at=blog.created_at,
                )
            )

    flagged_count = sum(1 for item in items if item.is_flagged)
    total = len(items)
    if flagged_only:
        items = [item for item in items if item.is_flagged]

    items.sort(key=lambda item: (item.created_at is None, item.created_at), reverse=True)

    return ApprovalQueueResponse(
        items=items,
        total=total,
        ready_for_review=total - flagged_count,
        flagged=flagged_count,
    )


@router.post(
    "/decision",
    response_model=ApprovalDecisionResponse,
    summary="Approve (or send back) any mix of posts and blogs in one call",
)
def decide_approval(
    payload: ApprovalDecisionRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Batch approval for the queue. Accepts post IDs and blog IDs together so the
    screen's "Batch Approve" can send one request for a mixed selection.
    """
    if not payload.post_ids and not payload.blog_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one post_id or blog_id.",
        )

    company = resolve_company(db, auth, company_id)

    posts = []
    if payload.post_ids:
        posts = (
            db.query(GeneratedPost)
            .filter(GeneratedPost.id.in_(payload.post_ids), GeneratedPost.company_id == company.id)
            .all()
        )
    blogs = []
    if payload.blog_ids:
        blogs = (
            db.query(GeneratedBlog)
            .filter(GeneratedBlog.id.in_(payload.blog_ids), GeneratedBlog.company_id == company.id)
            .all()
        )

    decided_at = datetime.now(timezone.utc)
    for item in (*posts, *blogs):
        # Stamp only the unapproved -> approved transition, so approving something
        # twice doesn't log a second approval.
        if payload.is_approved and not item.is_approved:
            item.approved_at = decided_at
        elif not payload.is_approved:
            item.approved_at = None
        item.is_approved = payload.is_approved
    db.commit()

    found_post_ids = [p.id for p in posts]
    found_blog_ids = [b.id for b in blogs]
    missing = [pid for pid in payload.post_ids if pid not in set(found_post_ids)]
    missing += [bid for bid in payload.blog_ids if bid not in set(found_blog_ids)]

    action = "Approved" if payload.is_approved else "Sent back to drafts"
    updated = len(found_post_ids) + len(found_blog_ids)
    return ApprovalDecisionResponse(
        status="success",
        message=f"{action} {updated} item(s).",
        updated_count=updated,
        updated_post_ids=found_post_ids,
        updated_blog_ids=found_blog_ids,
        missing_ids=missing,
    )
