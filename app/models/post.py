import uuid
from sqlalchemy import Column, String, Text, Boolean, Integer, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class GeneratedPost(Base):
    __tablename__ = "generated_posts"

    id = Column(String(50), primary_key=True, default=generate_uuid)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)

    prompt = Column(Text, nullable=False)
    platform = Column(String(50), nullable=False, index=True)

    title = Column(String(300), nullable=True)
    headline = Column(String(300), nullable=True)
    caption = Column(Text, nullable=False)
    hashtags = Column(Text, nullable=True)
    image_url = Column(String(500), nullable=True)
    reference_image_id = Column(String(100), nullable=True)
    reference_image_url = Column(String(500), nullable=True)

    tone = Column(String(50), nullable=True, default="Professional")
    language = Column(String(50), nullable=True, default="English (US)")
    ai_safety_score = Column(Integer, nullable=True, default=98)

    date = Column(String(50), nullable=True)
    start_time = Column(String(50), nullable=True)

    is_approved = Column(Boolean, default=False, nullable=False, server_default="false")
    is_posted = Column(Boolean, default=False, nullable=False, server_default="false")
    posted_at = Column(DateTime(timezone=True), nullable=True)
    post_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    company = relationship("Company")
