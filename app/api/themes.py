import asyncio
import io
import logging
import re
from pathlib import Path
from typing import Any, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.brand import BrandProfile
from app.schemas.brand import (
    BrandOptionsResponse,
    BrandProfileResponse,
    SaveBrandProfileRequest,
    StatusLiteral,
    VisualStyleLiteral,
)
from app.services.storage_service import upload_library_asset

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger("ThemesRoutes")

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_REFERENCE_UPLOAD_BYTES = 4 * 1024 * 1024
REFERENCE_UPLOAD_TYPES = {
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".webp": "image/webp",
}
MAX_PDF_TEXT_CHARS = 8000
MAX_PDF_TEXT_PAGES = 50
PDF_TEXT_TIMEOUT_SECONDS = 20
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _extract_pdf_text(content: bytes) -> Optional[str]:
    """
    A theme PDF's text, whitespace collapsed and capped, for generation to hand the
    text model as brand guidelines. None when pypdf is missing, "" when nothing is readable.
    """
    try:
        from pypdf import PdfReader
    except Exception as error:
        logger.warning("pypdf is unavailable, so theme PDF text was not extracted: %s", error)
        return None

    chunks: list[str] = []
    collected = 0
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            reader.decrypt("")
        for index, page in enumerate(reader.pages):
            # Collapsing whitespace shrinks the text, so read a little past the cap.
            if index >= MAX_PDF_TEXT_PAGES or collected > MAX_PDF_TEXT_CHARS * 2:
                break
            page_text = page.extract_text() or ""
            chunks.append(page_text)
            collected += len(page_text)
    except Exception as error:
        # Whatever was read before a broken page is still worth keeping.
        logger.warning("Could not read all the text of a theme PDF: %s", error)

    text = " ".join(_CONTROL_CHARS.sub(" ", " ".join(chunks)).split())
    # Lone surrogates from odd fonts cannot be stored as UTF-8.
    return text.encode("utf-8", "ignore").decode("utf-8")[:MAX_PDF_TEXT_CHARS]


async def _pdf_text_for_upload(content: bytes) -> Optional[str]:
    """Runs the extraction off the event loop and gives up on files that take too long."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _extract_pdf_text, content),
            timeout=PDF_TEXT_TIMEOUT_SECONDS,
        )
    except Exception as error:
        logger.warning("Theme PDF text extraction failed or timed out: %r", error)
        return ""


def _get_or_create(db: Session, company_id: int) -> BrandProfile:
    profile = db.query(BrandProfile).filter(BrandProfile.company_id == company_id).first()
    if profile is None:
        profile = BrandProfile(company_id=company_id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


@router.get("/options", response_model=BrandOptionsResponse, summary="The tone, font and visual style choices")
def get_options() -> Any:
    """So the dropdowns and style cards can be built from the API rather than hard-coded."""
    return BrandOptionsResponse()


@router.get("", response_model=BrandProfileResponse, summary="Get the company's brand profile")
def get_brand_profile(
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)
    return _get_or_create(db, company.id)


@router.put("", response_model=BrandProfileResponse, summary="Save the brand profile (Save Draft / Complete Setup)")
def save_brand_profile(
    payload: SaveBrandProfileRequest,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Save Draft keeps partially filled details; Complete Setup means the brand kit is
    ready to be applied to content, so the name and description have to be there.
    """
    company = resolve_company(db, auth, company_id)
    profile = _get_or_create(db, company.id)

    if payload.company_website:
        try:
            from app.services.knowledge_base_service import store_scraped_site
            store_scraped_site(db, company, payload.company_website, title=f"Company website for {profile.company_name or company.name or company.email}")
        except Exception as exc:
            logger.warning("Failed to scrape company website into knowledge base: %s", exc)

    data = payload.model_dump(exclude_unset=True, exclude={"status"})
    for field, value in data.items():
        setattr(profile, field, value.strip() if isinstance(value, str) else value)

    if payload.status == "complete":
        missing = [
            label
            for label, value in (("company name", profile.company_name), ("company description", profile.company_description))
            if not (value or "").strip()
        ]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Add the {' and '.join(missing)} before completing setup.",
            )
    profile.status = payload.status

    db.commit()
    db.refresh(profile)
    return profile


@router.patch("", response_model=BrandProfileResponse, summary="Partially update the brand profile and upload reference files")
async def patch_brand_profile(
    request: Request,
    company_name: Optional[str] = Form(None, max_length=255),
    company_description: Optional[str] = Form(None, max_length=5000),
    company_website: Optional[str] = Form(None, max_length=500),
    contact_email: Optional[str] = Form(None, max_length=255),
    contact_mobile: Optional[str] = Form(None, max_length=50),
    brand_tone: Optional[str] = Form(None, max_length=50),
    target_audience: Optional[str] = Form(None, max_length=500),
    visual_style: Optional[VisualStyleLiteral] = Form(None),
    brand_colors: Optional[str] = Form(
        None,
        description="Comma-separated hex colours, primary first (or the field repeated); replaces the saved list",
    ),
    custom_color: Optional[str] = Form(None),
    custom_text_style: Optional[str] = Form(None, max_length=255),
    custom_font: Optional[str] = Form(None, max_length=100),
    profile_status: Optional[StatusLiteral] = Form(None, alias="status"),
    files: list[UploadFile] = File(
        default=[],
        description="Optional PDFs and images; upload multiple files using the same field name",
        json_schema_extra={"items": {"type": "string", "format": "binary"}},
    ),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    form = await request.form()
    form_values = {
        "company_name": company_name,
        "company_description": company_description,
        "company_website": company_website,
        "contact_email": contact_email,
        "contact_mobile": contact_mobile,
        "brand_tone": brand_tone,
        "target_audience": target_audience,
        "visual_style": visual_style,
        # Read from the raw form so a field repeated once per colour keeps them all.
        "brand_colors": ",".join(value for value in form.getlist("brand_colors") if isinstance(value, str)),
        "custom_color": custom_color,
        "custom_text_style": custom_text_style,
        "custom_font": custom_font,
        "status": profile_status,
    }
    try:
        payload = SaveBrandProfileRequest.model_validate(
            {name: value for name, value in form_values.items() if name in form}
        )
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error

    company = resolve_company(db, auth, company_id)
    profile = _get_or_create(db, company.id)
    data = payload.model_dump(exclude_unset=True)
    requested_status = data.pop("status", None)
    if data.get("company_website"):
        try:
            from app.services.knowledge_base_service import store_scraped_site
            store_scraped_site(db, company, data["company_website"], title=f"Company website for {profile.company_name or company.name or company.email}")
        except Exception as exc:
            logger.warning("Failed to scrape company website into knowledge base: %s", exc)
    for field, value in data.items():
        setattr(profile, field, value.strip() if isinstance(value, str) else value)

    if requested_status == "complete":
        missing = [
            label
            for label, value in (("company name", profile.company_name), ("company description", profile.company_description))
            if not (value or "").strip()
        ]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Add the {' and '.join(missing)} before completing setup.",
            )

    prepared_uploads = []
    total_upload_bytes = 0
    for upload in files or []:
        filename = Path((upload.filename or "").replace("\\", "/")).name
        extension = Path(filename).suffix.lower()
        expected_content_type = REFERENCE_UPLOAD_TYPES.get(extension)
        content_type = (upload.content_type or expected_content_type or "").lower().split(";", 1)[0]
        if not expected_content_type or content_type not in {expected_content_type, "application/octet-stream"}:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Unsupported file '{filename}'. Upload PDF, PNG, JPEG, GIF, WebP, or AVIF files.",
            )

        remaining_bytes = MAX_REFERENCE_UPLOAD_BYTES - total_upload_bytes
        content = await upload.read(remaining_bytes + 1)
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The uploaded file '{filename}' is empty.",
            )
        total_upload_bytes += len(content)
        if total_upload_bytes > MAX_REFERENCE_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Combined reference files must be 4 MiB or smaller.",
            )
        prepared_uploads.append((filename, content, expected_content_type))

    uploaded_assets = []
    for filename, content, content_type in prepared_uploads:
        try:
            url = upload_library_asset(
                file_content=content,
                filename=filename,
                content_type=content_type,
            )
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
        asset = {"filename": filename, "content_type": content_type, "url": url}
        if content_type == "application/pdf":
            # Read once here so generation can use the guidelines without downloading the PDF again.
            text = await _pdf_text_for_upload(content)
            if text is not None:
                asset["text"] = text
        uploaded_assets.append(asset)

    if requested_status is not None:
        profile.status = requested_status
    if uploaded_assets:
        profile.reference_files = [*(profile.reference_files or []), *uploaded_assets]

    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/files", response_model=BrandProfileResponse, summary="Remove one uploaded theme file")
def delete_reference_file(
    url: str = Query(..., min_length=1, description="The file's url, exactly as listed in reference_files"),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """Takes the file off the brand profile so generation stops using it; the stored object is left alone."""
    company = resolve_company(db, auth, company_id)
    profile = db.query(BrandProfile).filter(BrandProfile.company_id == company.id).first()
    current = list(profile.reference_files or []) if profile is not None else []
    remaining = [entry for entry in current if not (isinstance(entry, dict) and entry.get("url") == url.strip())]
    if profile is None or len(remaining) == len(current):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That theme file was not found.")

    profile.reference_files = remaining
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/logo", response_model=BrandProfileResponse, summary="Upload or replace the company logo")
async def upload_logo(
    logo: UploadFile = File(..., description="Image file (JPG, PNG, WebP, SVG), up to 5 MB"),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    company = resolve_company(db, auth, company_id)

    content_type = (logo.content_type or "").lower().split(";", 1)[0]
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The logo must be an image file.")

    data = await logo.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The uploaded logo is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The logo must be 5 MB or smaller.")

    try:
        url = upload_library_asset(
            file_content=data,
            filename=logo.filename or "logo.png",
            content_type=content_type,
        )
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error

    profile = _get_or_create(db, company.id)
    profile.logo_url = url
    db.commit()
    db.refresh(profile)
    return profile
