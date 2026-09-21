from typing import Any
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core import deps, security
from app.models.companies import Company
from app.schemas.settings import (
    ChangePasswordRequest,
    MessageResponse,
    ProfileResponse,
    ProfileUpdateRequest,
)
from app.services.storage_service import upload_library_asset

router = APIRouter()

MAX_AVATAR_BYTES = 5 * 1024 * 1024


def _split_name(full_name: str | None) -> tuple[str, str]:
    parts = (full_name or "").strip().split(" ", 1)
    first = parts[0] if parts and parts[0] else ""
    last = parts[1].strip() if len(parts) > 1 else ""
    return first, last


def _to_profile(user: Company) -> ProfileResponse:
    # Accounts created before first/last name existed only have the combined name,
    # so fall back to splitting it until the user saves their own values.
    fallback_first, fallback_last = _split_name(user.name)
    first = user.first_name if user.first_name is not None else fallback_first
    last = user.last_name if user.last_name is not None else fallback_last
    return ProfileResponse(
        id=user.id,
        first_name=first,
        last_name=last,
        full_name=user.name,
        email=user.email,
        avatar_url=user.avatar_url,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
    )


@router.get("/profile", response_model=ProfileResponse, summary="Get the logged-in user's profile")
def get_profile(current_user: Company = Depends(deps.get_current_user)) -> Any:
    return _to_profile(current_user)


@router.put("/profile", response_model=ProfileResponse, summary="Update first and last name")
def update_profile(
    payload: ProfileUpdateRequest,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Email is intentionally not editable here - it is the login identity, so changing
    it is left to an administrator.
    """
    first = payload.first_name.strip()
    last = payload.last_name.strip()
    if not first or not last:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="First name and last name cannot be blank.",
        )

    current_user.first_name = first
    current_user.last_name = last
    # Keep the combined name in sync - it is what the rest of the app displays.
    current_user.name = f"{first} {last}"
    db.commit()
    db.refresh(current_user)
    return _to_profile(current_user)


@router.post("/profile/photo", response_model=ProfileResponse, summary="Upload or replace the profile photo")
async def upload_profile_photo(
    photo: UploadFile = File(..., description="Image file (JPG, PNG, WebP), up to 5 MB"),
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    content_type = (photo.content_type or "").lower().split(";", 1)[0]
    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile photo must be an image file.",
        )

    data = await photo.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded photo is empty.")
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile photo must be 5 MB or smaller.",
        )

    try:
        url = upload_library_asset(
            file_content=data,
            filename=photo.filename or "avatar.png",
            content_type=content_type,
        )
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error

    current_user.avatar_url = url
    db.commit()
    db.refresh(current_user)
    return _to_profile(current_user)


@router.post("/change-password", response_model=MessageResponse, summary="Change the password")
def change_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    if not security.verify_password(payload.old_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Old password is incorrect.",
        )
    if security.verify_password(payload.new_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the old password.",
        )

    current_user.hashed_password = security.get_password_hash(payload.new_password)
    db.commit()
    return MessageResponse(message="Password updated successfully.")
