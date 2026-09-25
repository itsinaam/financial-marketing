from sqlalchemy.orm import Session

from app.models.brand import BrandProfile

# How each visual style from the Themes screen should steer a generated image.
_STYLE_GUIDES = {
    "minimalist": "Clean, minimal composition with generous whitespace and a quiet, restrained palette.",
    "bold": "Punchy, high-contrast composition with confident, saturated colour.",
    "futuristic": "Dark, glowing, technical look with cool tones and a sense of advanced technology.",
}


def get_brand_profile(db: Session, company_id: int) -> BrandProfile | None:
    return db.query(BrandProfile).filter(BrandProfile.company_id == company_id).first()


def brand_prompt_context(brand: BrandProfile | None) -> str:
    """Brand details to hand the model so the writing sounds like the company."""
    if brand is None:
        return ""

    lines = []
    if (brand.company_name or "").strip():
        lines.append(f"BUSINESS: {brand.company_name.strip()}")
    if (brand.company_description or "").strip():
        lines.append(f"ABOUT THE BUSINESS: {brand.company_description.strip()}")
    if (brand.target_audience or "").strip():
        lines.append(f"AUDIENCE: {brand.target_audience.strip()}")
    return "\n".join(lines)


def brand_style_guide(brand: BrandProfile | None) -> str | None:
    """The image look the company picked, or None to leave the platform default alone."""
    if brand is None or not brand.visual_style:
        return None

    style = brand.visual_style.lower()
    if style != "custom":
        return _STYLE_GUIDES.get(style)

    parts = []
    if (brand.custom_color or "").strip():
        parts.append(f"Build the composition around the brand colour {brand.custom_color.strip()}.")
    if (brand.custom_text_style or "").strip():
        parts.append(brand.custom_text_style.strip())
    return " ".join(parts) or None
