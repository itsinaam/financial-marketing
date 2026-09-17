import json
import logging
import re
import requests
from sqlalchemy.orm import Session

from app.models.blog import GeneratedBlog
from app.models.credentials import Credentials
from app.services.image_embed_service import get_genai_client
from app.services.post_generator_service import find_relevant_library_image, generate_post_image
from app.services.storage_service import upload_library_asset
from app.services.wordpress_service import WordPressService
from app.services.ghost_service import GhostService

logger = logging.getLogger("BlogGeneratorService")

TEXT_MODEL = "gemini-3.6-flash"
_TAG_RE = re.compile(r"<[^>]+>")


def _fetch_reference_url_text(url: str, max_chars: int = 4000) -> str | None:
    """Fetch a reference URL and return a plain-text excerpt for style/context, or None on any failure."""
    try:
        res = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        text = _TAG_RE.sub(" ", res.text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text[:max_chars] if text else None
    except requests.RequestException as err:
        logger.warning("Failed to fetch reference URL '%s': %s", url, err)
        return None


def generate_blog_content(
    prompt: str,
    tone: str | None = "Professional",
    language: str | None = "English (US)",
    reference_url: str | None = None,
    extra_hashtags: list[str] | None = None,
) -> dict:
    """Generate a long-form blog title + HTML body + tags tailored to the topic."""
    tone_str = tone or "Professional"
    lang_str = language or "English (US)"

    reference_context = ""
    if reference_url:
        excerpt = _fetch_reference_url_text(reference_url)
        if excerpt:
            reference_context = f"\nREFERENCE ARTICLE (match this style, do not copy it verbatim):\n{excerpt}\n"

    prompt_text = f"""
You are an expert long-form content writer producing a blog article for a business.

TOPIC / REQUEST:
{prompt}
{reference_context}
TONE: {tone_str}
LANGUAGE: {lang_str}

Write a complete, well-structured blog post (600-900 words) with a few <h2> subheadings,
formatted as clean HTML (only <p>, <h2>, <ul>, <li>, <strong>, <em> tags - no <html>/<body>
wrapper, no inline styles, no markdown fences).

Return ONLY a JSON object (no markdown fences) with keys:
- "title": a compelling blog title (1 line) in {lang_str}
- "content": the full HTML body described above
- "hashtags": 4-8 relevant tags separated by spaces
- "ai_safety_score": integer 0-100 evaluating content safety/appropriateness
"""

    default_result = {
        "title": prompt[:80],
        "content": f"<p>{prompt}</p>",
        "hashtags": " ".join(extra_hashtags) if extra_hashtags else "",
        "ai_safety_score": 98,
    }

    try:
        client = get_genai_client()
        response = client.models.generate_content(model=TEXT_MODEL, contents=prompt_text)
        clean_json = response.text.strip()
        if clean_json.startswith("```"):
            clean_json = clean_json.strip("`").removeprefix("json").strip()
        data = json.loads(clean_json)

        try:
            score = int(data.get("ai_safety_score", 98))
        except (ValueError, TypeError):
            score = 98

        hashtags = (data.get("hashtags") or "").strip()
        if extra_hashtags:
            existing = set(hashtags.lower().split())
            merged = [hashtags] if hashtags else []
            merged += [tag for tag in extra_hashtags if tag.lower() not in existing]
            hashtags = " ".join(merged).strip()

        return {
            "title": data.get("title") or default_result["title"],
            "content": data.get("content") or default_result["content"],
            "hashtags": hashtags,
            "ai_safety_score": score,
        }
    except Exception as err:
        logger.warning("Failed to generate blog content via Gemini, using fallback: %s", err)
        return default_result


def create_generated_blog(
    db: Session,
    company_id: int,
    prompt: str,
    platform: str,
    date: str | None = None,
    start_time: str | None = None,
    tone: str | None = "Professional",
    language: str | None = "English (US)",
    hashtags: list[str] | None = None,
    reference_url: str | None = None,
    custom_image_data: dict | None = None,
) -> GeneratedBlog:
    """
    Full blog generation workflow for one target platform:
    1. Pick a reference image (user-uploaded, else best Library match) for the header image.
    2. Generate the title/body/tags tailored to the topic (optionally styled after reference_url).
    3. If a reference image is available, generate a new AI header image from it.
    4. Upload the generated image (if any) to Supabase storage.
    5. Save the draft as a GeneratedBlog row scoped to the company.
    """
    reference_id = None
    reference_image_url = None
    images_data: list[dict] = []

    if custom_image_data:
        images_data.append({"bytes": custom_image_data["bytes"], "mime_type": custom_image_data.get("mime_type", "image/png")})
        reference_id = custom_image_data.get("id")
        reference_image_url = custom_image_data.get("image_url")
    else:
        match = find_relevant_library_image(db, prompt, company_id)
        if match:
            reference_id = match["id"]
            reference_image_url = match["image_url"]
            try:
                res = requests.get(match["image_url"], timeout=30)
                res.raise_for_status()
                mime_type = "image/png" if match["image_url"].lower().endswith(".png") else "image/jpeg"
                images_data.append({"bytes": res.content, "mime_type": mime_type})
            except requests.RequestException as err:
                logger.warning("Failed to fetch matched library image bytes: %s", err)

    blog_data = generate_blog_content(
        prompt=prompt,
        tone=tone,
        language=language,
        reference_url=reference_url,
        extra_hashtags=hashtags,
    )

    image_url = None
    generated_bytes = generate_post_image(images_data, prompt, platform)
    if generated_bytes:
        try:
            image_url = upload_library_asset(
                file_content=generated_bytes,
                filename=f"blog_{platform}_{(reference_id or 'gen')[:8]}.png",
                content_type="image/png",
            )
        except Exception as err:
            logger.warning("Failed to upload generated blog image, saving draft without image: %s", err)

    blog = GeneratedBlog(
        company_id=company_id,
        prompt=prompt,
        platform=platform,
        title=blog_data["title"],
        content=blog_data["content"],
        hashtags=blog_data["hashtags"],
        image_url=image_url,
        reference_image_id=reference_id,
        reference_image_url=reference_image_url,
        reference_url=reference_url,
        date=date,
        start_time=start_time,
        tone=tone or "Professional",
        language=language or "English (US)",
        ai_safety_score=blog_data.get("ai_safety_score", 98),
        is_approved=False,
        is_posted=False,
    )
    db.add(blog)
    db.commit()
    db.refresh(blog)
    return blog


def publish_blog_to_platform(blog: GeneratedBlog, credential: Credentials | None) -> dict:
    """
    Publish an already-generated blog draft to its target platform. Raises
    RuntimeError with a user-facing message on any failure (missing auth,
    unsupported platform, API error).
    """
    plat = (blog.platform or "").lower().strip()

    if plat == "website":
        # The company's own site: no external API to call, the draft itself
        # becomes the published article (served via GET /api/blogs).
        return {"success": True, "target": "Website"}

    if plat == "wordpress":
        if not credential or not credential.access_token or not credential.organization_id:
            raise RuntimeError("WordPress site not connected. Save the site URL, username, and Application Password via POST /api/credentials/ first.")
        return WordPressService.create_post(
            site_url=credential.organization_id,
            username=credential.client_id,
            app_password=credential.access_token,
            title=blog.title or "Untitled",
            content=blog.content,
        )

    if plat == "ghost":
        if not credential or not credential.access_token or not credential.organization_id:
            raise RuntimeError("Ghost site not connected. Save the Admin API URL and Admin API Key via POST /api/credentials/ first.")
        return GhostService.create_post(
            admin_api_url=credential.organization_id,
            admin_api_key=credential.access_token,
            title=blog.title or "Untitled",
            html_content=blog.content,
        )

    if plat == "blogger":
        raise RuntimeError("Blogger publishing isn't set up yet (requires a Google Cloud OAuth app registration).")

    if plat == "medium":
        raise RuntimeError("Medium discontinued its public publishing API in 2023; direct publishing isn't possible.")

    if plat == "substack":
        raise RuntimeError("Substack has no public publishing API; direct publishing isn't possible.")

    raise RuntimeError(f"Platform '{plat}' is not supported for publishing yet.")
