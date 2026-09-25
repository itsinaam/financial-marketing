from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func
from app.core.database import Base


class BrandProfile(Base):
    """A company's brand kit: who they are, how they sound, and how their content looks."""

    __tablename__ = "brand_profiles"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, unique=True, index=True)

    logo_url = Column(String(500), nullable=True)
    company_name = Column(String(255), nullable=True)
    company_description = Column(Text, nullable=True)

    contact_email = Column(String(255), nullable=True)
    contact_mobile = Column(String(50), nullable=True)

    brand_tone = Column(String(50), nullable=True, default="Professional")
    target_audience = Column(String(500), nullable=True)

    visual_style = Column(String(20), nullable=True, default="minimalist")
    # Only meaningful when visual_style is "custom".
    custom_color = Column(String(20), nullable=True)
    custom_text_style = Column(String(255), nullable=True)
    custom_font = Column(String(100), nullable=True)

    # "draft" while Save Draft is used, "complete" once Complete Setup goes through.
    status = Column(String(20), nullable=False, default="draft", server_default="draft")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
