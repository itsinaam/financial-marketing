from __future__ import annotations

import enum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class KnowledgeBaseSource(str, enum.Enum):
    WEBSITE = "website"
    UPLOAD = "upload"


class KnowledgeBaseItem(Base):
    __tablename__ = "knowledge_base_items"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    source_type = Column(Enum(KnowledgeBaseSource), default=KnowledgeBaseSource.UPLOAD, nullable=False)
    source_name = Column(String(255), nullable=True)
    source_url = Column(String(500), nullable=True)
    title = Column(String(255), nullable=True)
    file_name = Column(String(255), nullable=True)
    # Size of the uploaded file in bytes (null for website pages and older uploads).
    file_size = Column(Integer, nullable=True)
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    company = relationship("Company", back_populates="knowledge_base_items")
