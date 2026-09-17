import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, ForeignKey, JSON, Integer
from sqlalchemy.sql import func
from app.core.database import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Library(Base):
    __tablename__ = "libraryy"

    id = Column(String(50), primary_key=True, default=generate_uuid)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    name = Column(String(200), nullable=False)
    type = Column(String(50), nullable=False)
    media_type = Column(String(20), nullable=False, default="photo", server_default="photo")
    image_url = Column(String(500), default=None)
    size = Column(Integer, default=None)
    embedding = Column(JSON, nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), default=get_utc_now, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=get_utc_now, onupdate=get_utc_now, server_default=func.now(), nullable=False)
