from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core import deps
from app.models.companies import Company
from app.models.knowledge_base import KnowledgeBaseItem, KnowledgeBaseSource
from app.schemas.knowledge_base import KnowledgeBaseItemResponse, WebsiteKnowledgeRequest
from app.services.knowledge_base_service import (
    delete_knowledge_base_asset,
    extract_text_from_uploaded_file,
    store_scraped_site,
)
from app.services.storage_service import upload_library_asset

router = APIRouter()


@router.get("", response_model=list[KnowledgeBaseItemResponse], summary="List knowledge-base items for the logged-in user")
def list_knowledge_base(
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
):
    items = (
        db.query(KnowledgeBaseItem)
        .filter(KnowledgeBaseItem.company_id == current_user.id)
        .order_by(KnowledgeBaseItem.created_at.desc())
        .all()
    )
    response_items = []
    for item in items:
        entry = KnowledgeBaseItemResponse.model_validate(item)
        if item.source_type == KnowledgeBaseSource.UPLOAD:
            entry.download_url = item.source_url or None
            if item.source_url:
                entry.source_url = item.source_url
        else:
            entry.download_url = item.source_url
        response_items.append(entry)
    return response_items


@router.post("/upload", response_model=KnowledgeBaseItemResponse, summary="Upload a text or PDF file into the knowledge base")
async def upload_knowledge_base_file(
    file: UploadFile = File(..., description="Text, Markdown, CSV, JSON, HTML, XML, PDF, or other supported data file"),
    title: str | None = Form(default=None),
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
):
    if file.filename is None or not file.filename.strip():
        raise HTTPException(status_code=400, detail="A filename is required.")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    try:
        extracted_text = extract_text_from_uploaded_file(file.filename, file_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not extracted_text.strip():
        raise HTTPException(status_code=400, detail="No readable text could be extracted from the uploaded file.")

    try:
        public_url = upload_library_asset(
            file_content=file_bytes,
            filename=file.filename,
            content_type=(file.content_type or "application/octet-stream").split(";", 1)[0],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"File upload failed: {exc}",
        ) from exc

    item = KnowledgeBaseItem(
        company_id=current_user.id,
        source_type=KnowledgeBaseSource.UPLOAD,
        source_name="upload",
        source_url=public_url,
        title=(title or Path(file.filename).stem or "Uploaded knowledge"),
        file_name=file.filename,
        file_size=len(file_bytes),
        content=extracted_text[:40000],
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    response = KnowledgeBaseItemResponse.model_validate(item)
    response.download_url = item.source_url
    return response


@router.delete("/{item_id}", status_code=status.HTTP_200_OK, summary="Delete a knowledge-base item")
def delete_knowledge_base_item(
    item_id: int,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
):
    item = (
        db.query(KnowledgeBaseItem)
        .filter(KnowledgeBaseItem.id == item_id, KnowledgeBaseItem.company_id == current_user.id)
        .first()
    )

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowledge-base item not found.",
        )

    try:
        delete_knowledge_base_asset(item)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not remove the stored file: {exc}",
        ) from exc

    db.delete(item)
    db.commit()

    return {"message": "Knowledge-base item deleted successfully."}
