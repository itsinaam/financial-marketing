from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.knowledge_base import KnowledgeBaseSource


class KnowledgeBaseItemResponse(BaseModel):
    id: int
    company_id: int
    source_type: KnowledgeBaseSource
    source_name: str | None = None
    source_url: str | None = None
    download_url: str | None = None
    title: str | None = None
    file_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class WebsiteKnowledgeRequest(BaseModel):
    url: str
    title: str | None = None
