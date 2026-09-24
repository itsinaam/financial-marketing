import re
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

VisualStyleLiteral = Literal["minimalist", "bold", "futuristic", "custom"]
StatusLiteral = Literal["draft", "complete"]

BRAND_TONES = ["Professional", "Casual", "Enthusiastic", "Informative", "Humorous"]
FONTS = ["Inter", "Roboto", "Poppins", "Montserrat", "Playfair Display", "Georgia"]

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class BrandProfileResponse(BaseModel):
    company_id: int
    logo_url: Optional[str] = None
    company_name: Optional[str] = None
    company_description: Optional[str] = None
    contact_email: Optional[str] = None
    contact_mobile: Optional[str] = None
    brand_tone: Optional[str] = None
    target_audience: Optional[str] = None
    visual_style: Optional[VisualStyleLiteral] = None
    custom_color: Optional[str] = None
    custom_text_style: Optional[str] = None
    custom_font: Optional[str] = None
    status: StatusLiteral = "draft"
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class SaveBrandProfileRequest(BaseModel):
    company_name: Optional[str] = Field(None, max_length=255)
    company_description: Optional[str] = Field(None, max_length=5000)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_mobile: Optional[str] = Field(None, max_length=50)
    brand_tone: Optional[str] = Field(
        None, max_length=50, description=f"One of: {', '.join(BRAND_TONES)}"
    )
    target_audience: Optional[str] = Field(None, max_length=500)
    visual_style: Optional[VisualStyleLiteral] = None
    custom_color: Optional[str] = Field(None, description="Hex colour such as #4F46E5; only used with the custom style")
    custom_text_style: Optional[str] = Field(None, max_length=255)
    custom_font: Optional[str] = Field(None, max_length=100, description=f"One of: {', '.join(FONTS)}")
    status: StatusLiteral = Field(
        "draft",
        description="'draft' for Save Draft, 'complete' for Complete Setup",
    )

    @field_validator("custom_color")
    @classmethod
    def valid_hex(cls, value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        if not _HEX_COLOR.match(value.strip()):
            raise ValueError("custom_color must be a hex colour such as #4F46E5.")
        return value.strip()


class BrandOptionsResponse(BaseModel):
    brand_tones: list[str] = BRAND_TONES
    fonts: list[str] = FONTS
    visual_styles: list[str] = ["minimalist", "bold", "futuristic", "custom"]
