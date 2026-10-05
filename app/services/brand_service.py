import importlib.util
import io
import logging
import re
import threading
from collections import OrderedDict

import requests
from sqlalchemy.orm import Session

from app.models.brand import BrandProfile

logger = logging.getLogger("BrandService")

# How each visual style from the Themes screen should steer a generated image.
_STYLE_GUIDES = {
    "minimalist": "Clean, minimal composition with generous whitespace and a quiet, restrained palette.",
    "bold": "Punchy, high-contrast composition with confident, saturated colour.",
    "futuristic": "Dark, glowing, technical look with cool tones and a sense of advanced technology.",
}

MAX_BRAND_COLORS = 8
_HEX_COLOR = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$")

# Brand files are fetched with short timeouts and size caps so a slow or oversized
# file can never hold up generation.
FETCH_TIMEOUT = (5, 10)
MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_THEME_FILE_BYTES = 4 * 1024 * 1024
MAX_STYLE_IMAGES = 3
MAX_STYLE_IMAGES_TOTAL_BYTES = 8 * 1024 * 1024
# A small file can still unpack to a huge image; anything bigger than this is skipped.
MAX_STYLE_IMAGE_PIXELS = 40_000_000
MAX_PDF_TEXT_CHARS = 8000
# All PDF guideline text handed to the text model, across every uploaded PDF.
MAX_GUIDELINE_CHARS = 6000

# Gemini's image model takes PNG/JPEG/WebP; GIFs are converted to PNG first.
_STYLE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_STYLE_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# Stored files never change behind their URL (every upload gets a new name), so the
# logo and theme files are kept in memory instead of re-downloaded for every post.
_CACHE_MAX_BYTES = 32 * 1024 * 1024
_cache: "OrderedDict[str, bytes | str]" = OrderedDict()
_cache_size = 0
_cache_lock = threading.Lock()


def get_brand_profile(db: Session, company_id: int) -> BrandProfile | None:
    return db.query(BrandProfile).filter(BrandProfile.company_id == company_id).first()


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_color(value) -> str | None:
    color = _clean(value).lower()
    if not _HEX_COLOR.match(color):
        return None
    if len(color) == 4:
        color = "#" + "".join(digit * 2 for digit in color[1:])
    return color


def _unique_colors(values) -> list[str]:
    colors = []
    for value in values:
        color = _normalize_color(value)
        if color and color not in colors:
            colors.append(color)
    return colors[:MAX_BRAND_COLORS]


def _color_list(value) -> list[str]:
    # Read like the Themes API does: a list, or a comma-separated string (older custom_color values).
    if isinstance(value, str):
        value = value.split(",")
    return _unique_colors(value) if isinstance(value, (list, tuple)) else []


def brand_colors(brand: BrandProfile | None) -> list[str]:
    """
    The saved palette as lowercase #rrggbb strings, first = primary. Rows saved before
    brand_colors existed only have custom_color, which then stands in as the palette.
    """
    if brand is None:
        return []
    return _color_list(getattr(brand, "brand_colors", None)) or _color_list(getattr(brand, "custom_color", None))


def theme_mode(brand: BrandProfile | None) -> str | None:
    """
    The Themes option in effect, decided the same way the Themes page shows it. Theme
    files are stored as soon as they are picked, without the mode, so a profile that is
    not explicitly "upload" or "custom" with colours, but has files, counts as "upload".
    Anything else keeps its saved style (a preset, "custom" or None).
    """
    if brand is None:
        return None
    style = _clean(brand.visual_style).lower()
    if style == "upload":
        return "upload"
    if style == "custom" and brand_colors(brand):
        return "custom"
    if getattr(brand, "reference_files", None):
        return "upload"
    return style or None


def is_upload_mode(brand: BrandProfile | None) -> bool:
    """True when the Themes page is on "Upload your theme"."""
    return theme_mode(brand) == "upload"


def _theme_files(brand: BrandProfile | None) -> list[dict]:
    files = getattr(brand, "reference_files", None) or []
    return [entry for entry in files if isinstance(entry, dict) and _clean(entry.get("url"))]


def _file_kind(entry: dict) -> str | None:
    """'pdf', 'image' (one the image model can use as a style reference) or None."""
    content_type = _clean(entry.get("content_type")).lower()
    if content_type:
        if content_type == "application/pdf":
            return "pdf"
        return "image" if content_type in _STYLE_IMAGE_TYPES else None

    name = (_clean(entry.get("filename")) or _clean(entry.get("url")).split("?", 1)[0]).lower()
    if name.endswith(".pdf"):
        return "pdf"
    return "image" if name.endswith(_STYLE_IMAGE_EXTENSIONS) else None


def _cache_get(key: str):
    with _cache_lock:
        value = _cache.get(key)
        if value is not None:
            _cache.move_to_end(key)
        return value


def _cache_put(key: str, value: bytes | str) -> None:
    global _cache_size
    with _cache_lock:
        previous = _cache.pop(key, None)
        if previous is not None:
            _cache_size -= len(previous)
        _cache[key] = value
        _cache_size += len(value)
        while _cache_size > _CACHE_MAX_BYTES and _cache:
            _, dropped = _cache.popitem(last=False)
            _cache_size -= len(dropped)


def _fetch_file(url: str, max_bytes: int, cache: bool = True) -> bytes | None:
    """Download a stored brand file (logo or theme file), or None if it is unreachable or too big."""
    key = f"file:{url}"
    cached = _cache_get(key) if cache else None
    if cached is not None:
        return cached

    try:
        with requests.get(url, timeout=FETCH_TIMEOUT, stream=True) as res:
            res.raise_for_status()
            data = bytearray()
            for chunk in res.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    logger.warning("Brand file '%s' is larger than %d bytes, skipping it.", url, max_bytes)
                    return None
    except Exception as err:
        logger.warning("Failed to fetch brand file '%s': %s", url, err)
        return None

    if not data:
        return None
    content = bytes(data)
    if cache:
        _cache_put(key, content)
    return content


def extract_pdf_text(data: bytes, max_chars: int = MAX_PDF_TEXT_CHARS) -> str | None:
    """
    The PDF's text, whitespace collapsed and capped at max_chars ("" for a scanned PDF
    with no text layer). None when pypdf is missing or the file can't be read.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf is not installed; PDF brand guidelines are skipped.")
        return None

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        length = 0
        for page in reader.pages:
            page_text = re.sub(r"\s+", " ", page.extract_text() or "").strip()
            if page_text:
                pages.append(page_text)
                length += len(page_text) + 1
            if length >= max_chars:
                break
        return " ".join(pages)[:max_chars]
    except Exception as err:
        logger.warning("Could not read the text of a PDF brand file: %s", err)
        return None


def _pdf_guideline_text(entry: dict) -> str:
    """The text saved with an uploaded PDF; PDFs uploaded before that was saved are read here once."""
    if isinstance(entry.get("text"), str):
        return entry["text"]

    url = _clean(entry.get("url"))
    key = f"pdf-text:{url}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if importlib.util.find_spec("pypdf") is None:
        logger.warning("pypdf is not installed; PDF brand guidelines are skipped.")
        return ""

    data = _fetch_file(url, MAX_THEME_FILE_BYTES, cache=False)
    text = extract_pdf_text(data) if data else None
    if text is None:
        return ""
    _cache_put(key, text)
    return text


def brand_guideline_text(brand: BrandProfile | None, max_chars: int = MAX_GUIDELINE_CHARS) -> str:
    """Text of the uploaded PDF brand guides (upload mode only), capped so the prompt stays small."""
    if not is_upload_mode(brand):
        return ""

    sections = []
    remaining = max_chars
    for entry in _theme_files(brand):
        if remaining <= 0:
            break
        if _file_kind(entry) != "pdf":
            continue
        text = _clean(_pdf_guideline_text(entry))[:remaining]
        if text:
            sections.append(f"[{_clean(entry.get('filename')) or 'Brand guide'}]\n{text}")
            remaining -= len(text)
    return "\n\n".join(sections)


def brand_prompt_context(
    brand: BrandProfile | None,
    company_description: str | None = None,
    target_audience: str | None = None,
) -> str:
    """
    Brand details to hand the text model so the writing sounds like the company.
    company_description / target_audience replace the saved ones when a request brings its own.
    """
    name = _clean(getattr(brand, "company_name", None))
    description = _clean(company_description) or _clean(getattr(brand, "company_description", None))
    audience = _clean(target_audience) or _clean(getattr(brand, "target_audience", None))
    website = _clean(getattr(brand, "company_website", None))
    mobile = _clean(getattr(brand, "contact_mobile", None))
    guidelines = brand_guideline_text(brand)

    lines = []
    if name:
        lines.append(f"BUSINESS: {name}")
    if description:
        lines.append(f"ABOUT THE BUSINESS: {description}")
    if audience:
        lines.append(f"AUDIENCE: {audience}")
    if website:
        lines.append(f"WEBSITE: {website}")
    if mobile:
        lines.append(f"PHONE: {mobile}")
    if not lines and not guidelines:
        return ""

    if website or mobile:
        lines.append(
            "CONTACT DETAILS: mention the website or phone number above only where a call to action "
            "fits naturally. Never invent any other contact details."
        )
    else:
        lines.append("CONTACT DETAILS: none supplied. Never invent a website, phone number, email or address.")
    if guidelines:
        lines.append(
            "BRAND GUIDELINES (from the company's uploaded brand files; follow them where they apply, "
            f"but the TONE line still sets the tone):\n{guidelines}"
        )
    return "\n".join(lines)


def brand_style_guide(brand: BrandProfile | None) -> str | None:
    """
    The brand look to add on top of the platform's own image style, or None to leave that
    style alone. Upload mode follows the uploaded theme images; custom mode uses the whole
    palette (first colour = primary); the presets are unchanged.
    """
    style = theme_mode(brand)
    if not style:
        return None
    if style == "upload":
        if not any(_file_kind(entry) == "image" for entry in _theme_files(brand)):
            return None
        return (
            "Follow the company's uploaded brand theme: where brand style reference images are supplied, "
            "match their colour palette, mood and overall visual style."
        )
    if style != "custom":
        return _STYLE_GUIDES.get(style)

    parts = []
    colors = brand_colors(brand)
    if len(colors) == 1:
        parts.append(f"Build the composition around the brand colour {colors[0]}.")
    elif colors:
        parts.append(
            f"Use the brand colour palette {', '.join(colors)}: build the composition around the primary "
            f"colour {colors[0]} and use the other colours as supporting accents."
        )
    if _clean(brand.custom_text_style):
        parts.append(_clean(brand.custom_text_style))
    return " ".join(parts) or None


def _sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _gif_to_png(data: bytes) -> bytes | None:
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow is not installed; GIF theme images are skipped.")
        return None

    try:
        with Image.open(io.BytesIO(data)) as image:
            out = io.BytesIO()
            image.convert("RGBA").save(out, format="PNG")
            return out.getvalue()
    except Exception as err:
        logger.warning("Could not convert a GIF theme image: %s", err)
        return None


def _is_readable_image(data: bytes) -> bool:
    """
    False when Pillow can't decode the image (or it is far too large). A broken file
    would otherwise reach the image model and fail the request. Without Pillow the
    bytes are trusted as before.
    """
    try:
        from PIL import Image
    except ImportError:
        return True

    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_STYLE_IMAGE_PIXELS:
                return False
            image.load()
        return True
    except Exception:
        return False


def load_brand_style_images(brand: BrandProfile | None) -> list[dict]:
    """
    Upload mode only: the newest uploaded theme images (up to MAX_STYLE_IMAGES) as
    {"bytes", "mime_type"}, for the image model to use as style references. Files that
    can't be fetched or aren't PNG/JPEG/WebP/GIF images are skipped.
    """
    if not is_upload_mode(brand):
        return []

    images = []
    total_bytes = 0
    for entry in reversed(_theme_files(brand)):
        if len(images) >= MAX_STYLE_IMAGES:
            break
        if _file_kind(entry) != "image":
            continue
        url = _clean(entry.get("url"))
        data = _fetch_file(url, MAX_THEME_FILE_BYTES)
        if not data:
            continue
        mime_type = _sniff_image_type(data)
        if mime_type == "image/gif":
            data = _gif_to_png(data)
            mime_type = "image/png" if data else None
        if not mime_type or not _is_readable_image(data):
            logger.warning("Theme file '%s' is not a readable PNG, JPEG, WebP or GIF image, skipping it.", url)
            continue
        if total_bytes + len(data) > MAX_STYLE_IMAGES_TOTAL_BYTES:
            continue
        images.append({"bytes": data, "mime_type": mime_type})
        total_bytes += len(data)
    return images


def stamp_logo(image_bytes: bytes, logo_bytes: bytes) -> bytes:
    """
    Place the logo small in the bottom-right corner (about 12% of the image's shorter side, at
    least 48px, ~3% padding), keeping its transparency. Returns PNG bytes, or the
    original bytes if Pillow is missing or either image can't be read.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("Pillow is not installed; saving the generated image without the logo.")
        return image_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as base_file, Image.open(io.BytesIO(logo_bytes)) as logo_file:
            keep_alpha = base_file.mode in ("RGBA", "LA", "PA") or "transparency" in base_file.info
            base = base_file.convert("RGBA")
            logo = ImageOps.exif_transpose(logo_file).convert("RGBA")

        width, height = base.size
        padding = max(1, round(min(width, height) * 0.03))
        # The logo fits a square box, so tall logos don't grow past the corner.
        # Sized from the shorter side, so a wide image doesn't get an oversized logo.
        box = min(max(48, round(min(width, height) * 0.12)), width - 2 * padding, height - 2 * padding)
        if box < 8:
            return image_bytes
        scale = min(box / logo.width, box / logo.height)
        size = (max(1, round(logo.width * scale)), max(1, round(logo.height * scale)))
        resample = getattr(Image, "Resampling", Image).LANCZOS
        logo = logo.resize(size, resample)

        base.alpha_composite(logo, (width - size[0] - padding, height - size[1] - padding))
        out = io.BytesIO()
        (base if keep_alpha else base.convert("RGB")).save(out, format="PNG")
        return out.getvalue()
    except Exception as err:
        logger.warning("Could not stamp the logo on the generated image, saving it without the logo: %s", err)
        return image_bytes


def apply_brand_logo(image_bytes: bytes, brand: BrandProfile | None) -> bytes:
    """Stamp the company logo on a generated image; the original bytes come back if there is no usable logo."""
    logo_url = _clean(getattr(brand, "logo_url", None))
    if not image_bytes or not logo_url:
        return image_bytes
    logo_bytes = _fetch_file(logo_url, MAX_LOGO_BYTES)
    if not logo_bytes:
        return image_bytes
    return stamp_logo(image_bytes, logo_bytes)
