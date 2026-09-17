import logging
from typing import  Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.api.credentials import resolve_company
from app.core.deps import get_db
from app.models.library import Library
from app.schemas.library import (
    LibraryDeleteResponse,
    LibraryItemResponse,
    LibraryListResponse,
    MediaTypeLiteral,
)
from app.services.image_embed_service import generate_image_embedding_from_bytes
from app.services.storage_service import upload_library_asset

logger = logging.getLogger("LibraryRoutes")

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)
MEDIA_TYPES = {"photo", "video", "article"}
ARTICLE_CONTENT_TYPES = {"application/pdf", "text/plain"}
ARTICLE_EXTENSIONS = {".pdf", ".txt"}
GENERIC_BINARY_CONTENT_TYPE = "application/octet-stream"


def serialize_library(item: Library) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "type": item.type,
        "media_type": item.media_type,
        "image_url": item.image_url,
        "media_url": item.image_url,
        "size": item.size,
        "size_kb": round((item.size or 0) / 1024, 2),
        "has_embedding": item.embedding is not None,
        "embedding": item.embedding,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }

def detect_media_type(media: UploadFile) -> str:
    """Work out whether an upload is a photo, video or article from the file itself."""
    content_type = (media.content_type or "").lower().split(";", 1)[0]
    filename = (media.filename or "").lower()

    if content_type.startswith("image/"):
        return "photo"
    if content_type.startswith("video/"):
        return "video"
    if content_type in ARTICLE_CONTENT_TYPES or filename.endswith(tuple(ARTICLE_EXTENSIONS)):
        return "article"

    raise HTTPException(
        status_code=400,
        detail="Unsupported file. Upload an image, a video, or a PDF/TXT article.",
    )


def validate_media_type(media_type: str) -> str:
    normalized_media_type = media_type.strip().lower()
    if normalized_media_type not in MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail="media_type must be one of: photo, video, article",
        )
    return normalized_media_type

def validate_media_file(media: UploadFile, media_type: str) -> None:
    content_type = (media.content_type or "").lower().split(";", 1)[0]
    filename = (media.filename or "").lower()

    if media_type == "photo" and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Photos must be image files")
    if media_type == "video" and not content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="Videos must be video files")
    if media_type == "article":
        if not filename.endswith(tuple(ARTICLE_EXTENSIONS)):
            raise HTTPException(status_code=400, detail="Articles must be PDF or TXT files")
        if content_type not in ARTICLE_CONTENT_TYPES | {GENERIC_BINARY_CONTENT_TYPE}:
            raise HTTPException(status_code=400, detail="Articles must be PDF or TXT files")

async def upload_media(media: UploadFile, media_type: str) -> tuple[str, int, bytes]:
    validate_media_file(media, media_type)

    media_content = await media.read()
    if not media_content:
        raise HTTPException(status_code=400, detail="Media file is empty")

    try:
        media_url = upload_library_asset(
            file_content=media_content,
            filename=media.filename or "media",
            content_type=media.content_type or "application/octet-stream",
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    return media_url, len(media_content), media_content

def get_library_or_404(library_id: str, db: Session, company=None) -> Library:
    library_item = db.query(Library).filter(Library.id == library_id).first()
    if not library_item:
        raise HTTPException(status_code=404, detail="Library item not found")
    if company is not None and library_item.company_id != company.id:
        raise HTTPException(status_code=403, detail="This asset does not belong to your account.")
    return library_item


@router.post(
    "",
    response_model=LibraryItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a library asset",
    description=(
        "Upload one asset using multipart/form-data. `media_type` is optional - when "
        "it is omitted it is detected from the uploaded file (image, video, or PDF/TXT)."
    ),
    response_description="The newly created library asset",
    responses={
        400: {"description": "Invalid media type or file type"},
        502: {"description": "Asset upload to storage failed"},
    },
)
async def create_library_item(
    name: str = Form(..., description="Display name for the asset", examples=["Product launch video"]),
    type: str = Form(..., description="Library category selected by the user", examples=["Robotics"]),
    media_type: Optional[MediaTypeLiteral] = Form(
        None, description="Asset kind: photo, video, or article. Detected from the file when omitted."
    ),
    media: UploadFile = File(
        ...,
        description="Image for photo, video file for video, or PDF/TXT file for article",
    ),
    company_id: Optional[int] = Form(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
):
    company = resolve_company(db, auth, company_id)
    normalized_media_type = validate_media_type(media_type) if media_type else detect_media_type(media)
    media_url, media_size, media_bytes = await upload_media(media, normalized_media_type)

    embedding = None
    if normalized_media_type == "photo":
        try:
            embedding = generate_image_embedding_from_bytes(
                image_bytes=media_bytes,
                mime_type=media.content_type or "image/jpeg",
            )
            logger.info("Generated image embedding successfully for asset: %s", name)
        except Exception as error:
            logger.warning("Failed to generate image embedding for '%s': %s", name, error)

    library_item = Library(
        company_id=company.id,
        name=name,
        type=type,
        media_type=normalized_media_type,
        image_url=media_url,
        size=media_size,
        embedding=embedding,
    )
    db.add(library_item)
    db.commit()
    db.refresh(library_item)
    return serialize_library(library_item)


@router.get(
    "",
    response_model=LibraryListResponse,
    summary="List library assets",
    description=(
        "Return the current company's library assets, newest first. Optionally filter the "
        "returned items by `type` and/or `media_type`; the total counts always include all "
        "of the company's assets."
    ),
    response_description="Library assets with media-type counts and storage totals",
    responses={400: {"description": "Invalid media type filter"}},
)
def list_library_items(
    media_type: Optional[MediaTypeLiteral] = Query(
        None,
        description="Optional filter for photo, video, or article assets",
        examples=["photo"],
    ),
    asset_type: Optional[str] = Query(
        None,
        alias="type",
        description="Optional exact-match library category filter",
        examples=["Robotics"],
    ),
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
):
    company = resolve_company(db, auth, company_id)
    owned = db.query(Library).filter(Library.company_id == company.id)

    query = owned
    if media_type is not None:
        query = query.filter(Library.media_type == validate_media_type(media_type))
    if asset_type is not None:
        query = query.filter(Library.type == asset_type)

    library_items = query.order_by(Library.created_at.desc()).all()
    all_items = owned.all()

    def count_of(kind: str) -> int:
        return sum(1 for item in all_items if item.media_type == kind)

    total_storage_bytes = sum(item.size or 0 for item in all_items)
    return {
        "items": [serialize_library(item) for item in library_items],
        "total_assets": len(all_items),
        "total_photo": count_of("photo"),
        "total_article": count_of("article"),
        "total_video": count_of("video"),
        "total_storage_bytes": total_storage_bytes,
        "total_storage_kb": round(total_storage_bytes / 1024, 2),
        "total_storage_mb": round(total_storage_bytes / (1024 * 1024), 2),
    }


@router.get(
    "/{library_id}",
    response_model=LibraryItemResponse,
    summary="Get a library asset by ID",
)
def get_library_item(
    library_id: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
):
    company = resolve_company(db, auth, company_id)
    return serialize_library(get_library_or_404(library_id, db, company))


@router.put(
    "/{library_id}",
    response_model=LibraryItemResponse,
    summary="Update a library asset",
)
async def update_library_item(
    library_id: str,
    name: Optional[str] = Form(None),
    type: Optional[str] = Form(None),
    media_type: Optional[str] = Form(None),
    media: Optional[UploadFile] = File(None),
    company_id: Optional[int] = Form(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
):
    company = resolve_company(db, auth, company_id)
    library_item = get_library_or_404(library_id, db, company)

    if name is not None:
        library_item.name = name
    if type is not None:
        library_item.type = type
    if media_type is not None:
        normalized_media_type = validate_media_type(media_type)
        if normalized_media_type != library_item.media_type and media is None:
            raise HTTPException(
                status_code=400,
                detail="Upload a replacement media file when changing media_type",
            )
        library_item.media_type = normalized_media_type
    if media is not None:
        media_url, media_size, media_bytes = await upload_media(
            media, library_item.media_type
        )
        library_item.image_url = media_url
        library_item.size = media_size
        if library_item.media_type == "photo":
            try:
                library_item.embedding = generate_image_embedding_from_bytes(
                    image_bytes=media_bytes,
                    mime_type=media.content_type or "image/jpeg",
                )
                logger.info("Updated image embedding for asset: %s", library_item.id)
            except Exception as error:
                logger.warning("Failed to update image embedding for asset '%s': %s", library_item.id, error)
        else:
            library_item.embedding = None

    db.commit()
    db.refresh(library_item)
    return serialize_library(library_item)


@router.delete(
    "/{library_id}",
    response_model=LibraryDeleteResponse,
    summary="Delete a library asset",
)
def delete_library_item(
    library_id: str,
    company_id: Optional[int] = Query(None, description="Optional company ID override (Admin / Testing)"),
    db: Session = Depends(get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
):
    company = resolve_company(db, auth, company_id)
    library_item = get_library_or_404(library_id, db, company)
    db.delete(library_item)
    db.commit()
    return {"message": "Library item deleted successfully", "id": library_id}
