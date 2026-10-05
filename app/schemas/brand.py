import re
from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# "upload" means the uploaded theme files drive the look; "custom" means the picked colours do.
VisualStyleLiteral = Literal["minimalist", "bold", "futuristic", "custom", "upload"]
StatusLiteral = Literal["draft", "complete"]

BRAND_TONES = ["Professional", "Casual", "Enthusiastic", "Informative", "Humorous"]
FONTS = ["Inter", "Roboto", "Poppins", "Montserrat", "Playfair Display", "Georgia"]
MAX_BRAND_COLORS = 8

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_hex_color(value: Any) -> Optional[str]:
    """The colour as lowercase #rrggbb, or None when it is not a hex colour."""
    if not isinstance(value, str) or not _HEX_COLOR.match(value.strip()):
        return None
    color = value.strip().lower()
    if len(color) == 4:
        color = "#" + "".join(digit * 2 for digit in color[1:])
    return color


def _clean_colors(value: Any) -> list[str]:
    """Readable colours from a list or a comma-separated string, de-duplicated in order; anything else is dropped."""
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    colors: list[str] = []
    for raw in value:
        color = normalize_hex_color(raw)
        if color and color not in colors:
            colors.append(color)
            if len(colors) == MAX_BRAND_COLORS:
                break
    return colors


def effective_brand_colors(brand_colors: Any, custom_color: Optional[str]) -> list[str]:
    """
    The colours a saved profile really has. Rows saved before several colours
    could be kept only have custom_color, so that stands in when brand_colors is empty.
    """
    return _clean_colors(brand_colors) or _clean_colors(custom_color)


class BrandProfileResponse(BaseModel):
    company_id: int
    logo_url: Optional[str] = None
    reference_files: list[dict[str, str]] = Field(default_factory=list)
    company_name: Optional[str] = None
    company_description: Optional[str] = None
    company_website: Optional[str] = None
    contact_email: Optional[str] = None
    contact_mobile: Optional[str] = None
    brand_tone: Optional[str] = None
    target_audience: Optional[str] = None
    visual_style: Optional[VisualStyleLiteral] = None
    brand_colors: list[str] = Field(default_factory=list)
    custom_color: Optional[str] = None
    custom_text_style: Optional[str] = None
    custom_font: Optional[str] = None
    status: StatusLiteral = "draft"
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("brand_colors", mode="before")
    @classmethod
    def readable_brand_colors(cls, value: Any) -> list[str]:
        return _clean_colors(value)

    @model_validator(mode="after")
    def legacy_brand_colors(self) -> "BrandProfileResponse":
        self.brand_colors = effective_brand_colors(self.brand_colors, self.custom_color)
        return self


class SaveBrandProfileRequest(BaseModel):
    company_name: Optional[str] = Field(None, max_length=255)
    company_description: Optional[str] = Field(None, max_length=5000)
    company_website: Optional[str] = Field(None, max_length=500)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_mobile: Optional[str] = Field(None, max_length=50)
    brand_tone: Optional[str] = Field(
        None, max_length=50, description=f"One of: {', '.join(BRAND_TONES)}"
    )
    target_audience: Optional[str] = Field(None, max_length=500)
    visual_style: Optional[VisualStyleLiteral] = None
    brand_colors: Optional[list[str]] = Field(
        None,
        description=(
            f"Up to {MAX_BRAND_COLORS} hex colours such as #4F46E5, primary first; only used with the custom style. "
            "Replaces the saved list."
        ),
    )
    custom_color: Optional[str] = Field(
        None,
        description="Hex colour such as #4F46E5; only used with the custom style. Kept for older clients - "
        "when brand_colors is sent this is set to its first colour",
    )
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
        color = normalize_hex_color(value)
        if color is None:
            raise ValueError("custom_color must be a hex colour such as #4F46E5.")
        return color

    @field_validator("brand_colors", mode="before")
    @classmethod
    def split_brand_colors(cls, value: Any) -> Any:
        # Multipart forms send the list as one comma-separated field.
        if isinstance(value, str):
            value = [part for part in value.split(",") if part.strip()]
        # Turned away before each entry is checked, so an oversized list costs next to nothing.
        if isinstance(value, (list, tuple)) and len(value) > MAX_BRAND_COLORS * 8:
            raise ValueError(f"brand_colors can hold at most {MAX_BRAND_COLORS} colours.")
        return value

    @field_validator("brand_colors")
    @classmethod
    def valid_brand_colors(cls, value: Optional[list[str]]) -> list[str]:
        colors: list[str] = []
        for raw in value or []:
            color = normalize_hex_color(raw)
            if color is None:
                raise ValueError(f"brand_colors must be hex colours such as #4F46E5; '{raw.strip()}' is not.")
            if color not in colors:
                colors.append(color)
                # Checked as it grows, so the list never gets long enough to make this loop slow.
                if len(colors) > MAX_BRAND_COLORS:
                    raise ValueError(f"brand_colors can hold at most {MAX_BRAND_COLORS} colours.")
        return colors

    @model_validator(mode="after")
    def sync_colors(self) -> "SaveBrandProfileRequest":
        # custom_color always mirrors the primary brand colour, so a client that
        # only knows custom_color still keeps brand_colors right, and vice versa.
        if "brand_colors" in self.model_fields_set:
            self.custom_color = self.brand_colors[0] if self.brand_colors else None
        elif "custom_color" in self.model_fields_set:
            self.brand_colors = [self.custom_color] if self.custom_color else []
        return self


class BrandOptionsResponse(BaseModel):
    brand_tones: list[str] = BRAND_TONES
    fonts: list[str] = FONTS
    visual_styles: list[str] = ["minimalist", "bold", "futuristic", "custom", "upload"]
