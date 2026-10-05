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
        uploaded_assets.append({"filename": filename, "content_type": content_type, "url": url})

    if requested_status is not None:
        profile.status = requested_status
    if uploaded_assets:
        profile.reference_files = [*(profile.reference_files or []), *uploaded_assets]

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
