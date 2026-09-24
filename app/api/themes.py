from typing import Any, Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core import deps
from app.api.credentials import resolve_company
from app.models.brand import BrandProfile
from app.schemas.brand import (
    BrandOptionsResponse,
    BrandProfileResponse,
    SaveBrandProfileRequest,
)
from app.services.storage_service import upload_library_asset

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

MAX_LOGO_BYTES = 5 * 1024 * 1024


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
