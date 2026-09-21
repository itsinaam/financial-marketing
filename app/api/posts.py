import re
from datetime import datetime, timezone
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.credentials import Credentials
from app.models.post import GeneratedPost
from app.schemas.posts import (
    ApprovePostsRequest,
    ApprovePostsResponse,
    EditPostRequest,
    GeneratedPostResponse,
    GeneratePostResult,
    PublishPostResponse,
)
from app.services.post_generator_service import create_generated_post, publish_post_to_platform
from app.services.storage_service import upload_library_asset

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

SUPPORTED_PLATFORMS = {"linkedin", "instagram", "facebook", "x"}

# Safety cap so a prompt like "generate 20 posts" can't blow past the Gemini
# image-generation rate limit (10 images/minute on the paid tier) or run away
# in cost in a single request.
MAX_TOTAL_GENERATIONS_PER_REQUEST = 10

_COUNT_KEYWORDS = r"posts?|variations?|drafts?|designs?|images?"
_COUNT_PATTERN = re.compile(
    rf"\b(\d{{1,2}})\s*(?:{_COUNT_KEYWORDS})\b|\b(?:{_COUNT_KEYWORDS})\s*[: ]?\s*(\d{{1,2}})\b",
    re.IGNORECASE,
)


def _extract_post_count(prompt: str, max_count: int = MAX_TOTAL_GENERATIONS_PER_REQUEST) -> tuple[str, int]:
    """
    Look for a quantity phrase like "5 posts" / "banao 5 posts" / "posts: 3" inside
    the free-text prompt. Returns (topic_prompt_with_quantity_phrase_removed, count).
    Defaults to count=1 when no quantity phrase is found.
    """
    match = _COUNT_PATTERN.search(prompt)
    if not match:
        return prompt.strip(), 1

    count_str = match.group(1) or match.group(2)
    count = max(1, min(int(count_str), max_count))

    cleaned = _COUNT_PATTERN.sub(" ", prompt, count=1)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return (cleaned or prompt.strip()), count


def _parse_platforms(platforms: str) -> List[str]:
    result = []
    for raw in platforms.replace(";", ",").split(","):
        name = raw.strip().lower()
        if name == "twitter":
            name = "x"
        if name and name not in result:
            result.append(name)

    unsupported = [p for p in result if p not in SUPPORTED_PLATFORMS]
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform(s): {', '.join(unsupported)}. Supported: linkedin, instagram, facebook, x.",
        )
    if not result:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one platform must be selected.")
    return result


def _parse_hashtags(hashtags: Optional[str]) -> List[str]:
    if not hashtags:
        return []
    tags = []
    for raw in hashtags.replace(",", " ").split():
        tag = raw.strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = f"#{tag}"
        if tag not in tags:
            tags.append(tag)
    return tags


@router.post(
    "/generate",
    response_model=GeneratePostResult,
    status_code=status.HTTP_201_CREATED,
    summary="Generate AI post draft(s) for one or more platforms",
)
async def generate_posts(
    prompt: str = Form(..., description="Describe the post in detail", max_length=2000),
    platforms: str = Form(..., description="Comma-separated target platforms (linkedin, instagram, facebook, x)"),
    tone: str = Form("Professional", description="Writing tone, e.g. Professional, Casual"),
    language: str = Form("English (US)", description="Output language"),
    hashtags: Optional[str] = Form(None, description="Optional comma/space-separated hashtags to include"),
    date: Optional[str] = Form(None, description="Scheduled date (YYYY-MM-DD)"),
    start_time: Optional[str] = Form(None, description="Scheduled post time"),
    images: Optional[List[UploadFile]] = File(None, description="Optional reference image(s) to use instead of the library search"),
    company_id: Optional[int] = Form(None, description="Optional company ID override (defaults to current logged in user)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    For each selected platform, finds a relevant reference image (from an uploaded
    image or the shared Library via AI similarity search), generates a caption,
    hashtags, and a new post image via Gemini, and saves the result as an
    unapproved draft.

    If the prompt itself contains a quantity phrase (e.g. "generate 5 posts about
    our new launch"), that many distinct variations are generated per platform
    (each with its own AI-written caption and AI-generated image), capped at
    MAX_TOTAL_GENERATIONS_PER_REQUEST total across all platforms combined.
    """
    if not prompt or not prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Prompt cannot be empty.")

    company = resolve_company(db, auth, company_id)
    target_platforms = _parse_platforms(platforms)
    extra_hashtags = _parse_hashtags(hashtags)
    topic_prompt, requested_count = _extract_post_count(prompt.strip())

    # Cap combined (platforms x count) so one request can't exceed the safety limit.
    count_per_platform = max(1, min(requested_count, MAX_TOTAL_GENERATIONS_PER_REQUEST // len(target_platforms)))

    custom_images_data = []
    if images:
        for idx, file in enumerate(images, 1):
            if not file.filename:
                continue
            file_bytes = await file.read()
            if not file_bytes:
                continue
            content_type = file.content_type or "image/png"
            public_url = upload_library_asset(
                file_content=file_bytes,
                filename=file.filename,
                content_type=content_type,
            )
            custom_images_data.append({
                "id": f"uploaded-{idx}",
                "name": file.filename,
                "image_url": public_url,
                "bytes": file_bytes,
                "mime_type": content_type,
            })

    generated_posts = []
    for platform in target_platforms:
        for _ in range(count_per_platform):
            post = create_generated_post(
                db=db,
                company_id=company.id,
                prompt=topic_prompt,
                platform=platform,
                date=date,
                start_time=start_time,
                tone=tone,
                language=language,
                hashtags=extra_hashtags or None,
                custom_images_data=custom_images_data or None,
            )
            generated_posts.append(post)

    note = None
    if count_per_platform < requested_count:
        note = (
            f"Requested {requested_count} post(s) per platform, but capped at {count_per_platform} per "
            f"platform ({len(generated_posts)} total) to stay within the AI generation rate limit."
        )

    return GeneratePostResult(status="success", count=len(generated_posts), posts=generated_posts, note=note)


@router.get("", response_model=List[GeneratedPostResponse], summary="List generated posts for the current company")
def list_posts(
    is_approved: Optional[bool] = Query(None),
    is_posted: Optional[bool] = Query(None),
    platform: Optional[str] = Query(None),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    query = db.query(GeneratedPost).filter(GeneratedPost.company_id == company.id)
    if is_approved is not None:
        query = query.filter(GeneratedPost.is_approved == is_approved)
    if is_posted is not None:
        query = query.filter(GeneratedPost.is_posted == is_posted)
    if platform:
        query = query.filter(GeneratedPost.platform == platform.lower().strip())
    return query.order_by(GeneratedPost.created_at.desc()).all()


@router.get("/calendar", response_model=List[GeneratedPostResponse], summary="Get approved posts for the calendar view")
def get_calendar_posts(
    is_posted: Optional[bool] = Query(None),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    query = db.query(GeneratedPost).filter(
        GeneratedPost.company_id == company.id,
        GeneratedPost.is_approved == True,  # noqa: E712
    )
    if is_posted is not None:
        query = query.filter(GeneratedPost.is_posted == is_posted)
    return query.order_by(GeneratedPost.date.asc(), GeneratedPost.start_time.asc()).all()


def _get_owned_post(db: Session, post_id: str, company) -> GeneratedPost:
    post = db.query(GeneratedPost).filter(GeneratedPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Generated post '{post_id}' not found.")
    if post.company_id != company.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This post does not belong to your account.")
    return post


@router.get("/{post_id}", response_model=GeneratedPostResponse, summary="Get a single generated post by ID")
def get_post(
    post_id: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    return _get_owned_post(db, post_id, company)


@router.put("/{post_id}", response_model=GeneratedPostResponse, summary="Edit caption, hashtags, and details of a generated post")
def edit_post(
    post_id: str,
    payload: EditPostRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    post = _get_owned_post(db, post_id, company)

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(post, field, value)

    db.commit()
    db.refresh(post)
    return post


@router.delete("/{post_id}", summary="Delete a generated post")
def delete_post(
    post_id: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    post = _get_owned_post(db, post_id, company)
    db.delete(post)
    db.commit()
    return {"status": "success", "message": f"Generated post '{post_id}' deleted successfully.", "id": post_id}


@router.post("/approve", response_model=ApprovePostsResponse, summary="Approve (or unapprove) one or more generated posts")
def approve_posts(
    payload: ApprovePostsRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    if not payload.post_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="post_ids list cannot be empty.")

    company = resolve_company(db, auth, company_id)
    posts = (
        db.query(GeneratedPost)
        .filter(GeneratedPost.id.in_(payload.post_ids), GeneratedPost.company_id == company.id)
        .all()
    )
    found_ids = {p.id for p in posts}
    missing_ids = [pid for pid in payload.post_ids if pid not in found_ids]

    decided_at = datetime.now(timezone.utc)
    for post in posts:
        # Stamp only the unapproved -> approved transition, so approving something
        # twice doesn't log a second approval.
        if payload.is_approved and not post.is_approved:
            post.approved_at = decided_at
        elif not payload.is_approved:
            post.approved_at = None
        post.is_approved = payload.is_approved
    db.commit()

    return ApprovePostsResponse(
        status="success",
        message=f"Updated approval status to {payload.is_approved} for {len(posts)} post(s).",
        approved_count=len(posts),
        approved_ids=list(found_ids),
        missing_ids=missing_ids,
    )


@router.post("/{post_id}/publish", response_model=PublishPostResponse, summary="Manually publish an approved post to its target platform")
def publish_post(
    post_id: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    post = _get_owned_post(db, post_id, company)

    if post.is_posted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This post has already been published.")

    credential = (
        db.query(Credentials)
        .filter(Credentials.company_id == company.id, Credentials.platform == post.platform)
        .first()
    )
    if not credential:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No credentials found for platform '{post.platform}'. Connect the account via POST /api/credentials/ first.",
        )

    try:
        result = publish_post_to_platform(post, credential)
    except RuntimeError as err:
        post.post_error = str(err)
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))

    post.is_posted = True
    post.posted_at = datetime.now(timezone.utc)
    post.post_error = None
    db.commit()

    return PublishPostResponse(
        status="success",
        message=f"Post published successfully to {post.platform}.",
        post_id=post.id,
        details=result,
    )
