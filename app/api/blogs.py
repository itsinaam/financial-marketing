from datetime import datetime, timezone
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.credentials import Credentials
from app.models.blog import GeneratedBlog
from app.schemas.blogs import (
    ApproveBlogsRequest,
    ApproveBlogsResponse,
    EditBlogRequest,
    GeneratedBlogResponse,
    GenerateBlogResult,
    PublishBlogResponse,
)
from app.services.blog_generator_service import create_generated_blog, publish_blog_to_platform
from app.services.storage_service import upload_library_asset

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

SUPPORTED_PLATFORMS = {"website", "medium", "wordpress", "blogger", "substack", "ghost"}


def _parse_platforms(platforms: str) -> List[str]:
    result = []
    for raw in platforms.replace(";", ",").split(","):
        name = raw.strip().lower()
        if name and name not in result:
            result.append(name)

    unsupported = [p for p in result if p not in SUPPORTED_PLATFORMS]
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform(s): {', '.join(unsupported)}. Supported: {', '.join(sorted(SUPPORTED_PLATFORMS))}.",
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
    response_model=GenerateBlogResult,
    status_code=status.HTTP_201_CREATED,
    summary="Generate AI blog draft(s) for one or more platforms",
)
async def generate_blogs(
    prompt: str = Form(..., description="Describe the blog post in detail", max_length=2000),
    platforms: str = Form(..., description="Comma-separated target platforms (website, medium, wordpress, blogger, substack, ghost)"),
    tone: str = Form("Professional", description="Writing tone, e.g. Professional, Casual"),
    language: str = Form("English (US)", description="Output language"),
    hashtags: Optional[str] = Form(None, description="Optional comma/space-separated tags to include"),
    reference_url: Optional[str] = Form(None, description="Optional URL for the AI to use as style/context reference"),
    date: Optional[str] = Form(None, description="Scheduled date (YYYY-MM-DD)"),
    start_time: Optional[str] = Form(None, description="Scheduled post time"),
    image: Optional[UploadFile] = File(None, description="Optional reference image to use instead of the library search"),
    company_id: Optional[int] = Form(None, description="Optional company ID override (defaults to current logged in user)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    For each selected platform, finds a reference image (from an uploaded image or the
    shared Library via AI similarity search), generates a long-form blog title, HTML body,
    and tags, and saves the result as an unapproved draft.
    """
    if not prompt or not prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Prompt cannot be empty.")

    company = resolve_company(db, auth, company_id)
    target_platforms = _parse_platforms(platforms)
    extra_hashtags = _parse_hashtags(hashtags)

    custom_image_data = None
    if image and image.filename:
        file_bytes = await image.read()
        if file_bytes:
            content_type = image.content_type or "image/png"
            public_url = upload_library_asset(
                file_content=file_bytes,
                filename=image.filename,
                content_type=content_type,
            )
            custom_image_data = {
                "id": "uploaded-1",
                "image_url": public_url,
                "bytes": file_bytes,
                "mime_type": content_type,
            }

    generated_blogs = []
    for platform in target_platforms:
        blog = create_generated_blog(
            db=db,
            company_id=company.id,
            prompt=prompt.strip(),
            platform=platform,
            date=date,
            start_time=start_time,
            tone=tone,
            language=language,
            hashtags=extra_hashtags or None,
            reference_url=reference_url,
            custom_image_data=custom_image_data,
        )
        generated_blogs.append(blog)

    return GenerateBlogResult(status="success", count=len(generated_blogs), blogs=generated_blogs)


@router.get("", response_model=List[GeneratedBlogResponse], summary="List generated blogs for the current company")
def list_blogs(
    is_approved: Optional[bool] = Query(None),
    is_posted: Optional[bool] = Query(None),
    platform: Optional[str] = Query(None),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    query = db.query(GeneratedBlog).filter(GeneratedBlog.company_id == company.id)
    if is_approved is not None:
        query = query.filter(GeneratedBlog.is_approved == is_approved)
    if is_posted is not None:
        query = query.filter(GeneratedBlog.is_posted == is_posted)
    if platform:
        query = query.filter(GeneratedBlog.platform == platform.lower().strip())
    return query.order_by(GeneratedBlog.created_at.desc()).all()


@router.get("/calendar", response_model=List[GeneratedBlogResponse], summary="Get approved blogs for the calendar view")
def get_calendar_blogs(
    is_posted: Optional[bool] = Query(None),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    query = db.query(GeneratedBlog).filter(
        GeneratedBlog.company_id == company.id,
        GeneratedBlog.is_approved == True,  # noqa: E712
    )
    if is_posted is not None:
        query = query.filter(GeneratedBlog.is_posted == is_posted)
    return query.order_by(GeneratedBlog.date.asc(), GeneratedBlog.start_time.asc()).all()


def _get_owned_blog(db: Session, blog_id: str, company) -> GeneratedBlog:
    blog = db.query(GeneratedBlog).filter(GeneratedBlog.id == blog_id).first()
    if not blog:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Generated blog '{blog_id}' not found.")
    if blog.company_id != company.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This blog does not belong to your account.")
    return blog


@router.get("/{blog_id}", response_model=GeneratedBlogResponse, summary="Get a single generated blog by ID")
def get_blog(
    blog_id: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    return _get_owned_blog(db, blog_id, company)


@router.put("/{blog_id}", response_model=GeneratedBlogResponse, summary="Edit title, content, tags, and details of a generated blog")
def edit_blog(
    blog_id: str,
    payload: EditBlogRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    blog = _get_owned_blog(db, blog_id, company)

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(blog, field, value)

    db.commit()
    db.refresh(blog)
    return blog


@router.delete("/{blog_id}", summary="Delete a generated blog")
def delete_blog(
    blog_id: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    blog = _get_owned_blog(db, blog_id, company)
    db.delete(blog)
    db.commit()
    return {"status": "success", "message": f"Generated blog '{blog_id}' deleted successfully.", "id": blog_id}


@router.post("/approve", response_model=ApproveBlogsResponse, summary="Approve (or unapprove) one or more generated blogs")
def approve_blogs(
    payload: ApproveBlogsRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    if not payload.blog_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="blog_ids list cannot be empty.")

    company = resolve_company(db, auth, company_id)
    blogs = (
        db.query(GeneratedBlog)
        .filter(GeneratedBlog.id.in_(payload.blog_ids), GeneratedBlog.company_id == company.id)
        .all()
    )
    found_ids = {b.id for b in blogs}
    missing_ids = [bid for bid in payload.blog_ids if bid not in found_ids]

    for blog in blogs:
        blog.is_approved = payload.is_approved
    db.commit()

    return ApproveBlogsResponse(
        status="success",
        message=f"Updated approval status to {payload.is_approved} for {len(blogs)} blog(s).",
        approved_count=len(blogs),
        approved_ids=list(found_ids),
        missing_ids=missing_ids,
    )


@router.post("/{blog_id}/publish", response_model=PublishBlogResponse, summary="Manually publish an approved blog to its target platform")
def publish_blog(
    blog_id: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    blog = _get_owned_blog(db, blog_id, company)

    if blog.is_posted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This blog has already been published.")

    credential = (
        db.query(Credentials)
        .filter(Credentials.company_id == company.id, Credentials.platform == blog.platform)
        .first()
    )

    try:
        result = publish_blog_to_platform(blog, credential)
    except RuntimeError as err:
        blog.post_error = str(err)
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))

    blog.is_posted = True
    blog.posted_at = datetime.now(timezone.utc)
    blog.post_error = None
    db.commit()

    return PublishBlogResponse(
        status="success",
        message=f"Blog published successfully to {blog.platform}.",
        blog_id=blog.id,
        details=result,
    )
