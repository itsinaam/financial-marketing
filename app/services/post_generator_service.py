import json
import logging
import requests
from datetime import datetime
from google.genai import errors, types
from sqlalchemy.orm import Session

from app.models.credentials import Credentials
from app.models.library import Library
from app.models.post import GeneratedPost
from app.services.image_embed_service import get_genai_client
from app.services.text_embed_service import generate_text_embedding, cosine_similarity
from app.services.storage_service import upload_library_asset
from app.services.brand_service import (
    apply_brand_logo,
    brand_prompt_context,
    brand_style_guide,
    get_brand_profile,
    load_brand_style_images,
)
from app.services.linkedin_service import LinkedInService
from app.services.instagram_service import InstagramService
from app.services.facebook_service import FacebookService
from app.services.twitter_service import TwitterService

logger = logging.getLogger("PostGeneratorService")

TEXT_MODEL = "gemini-3.6-flash"
IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_MATCH_THRESHOLD = 0.20

DEFAULT_IMAGE_SYSTEM_PROMPT = (
    "You are an expert social media graphic designer. Generate a high quality, "
    "professional social media post image based on the user's request and any "
    "supplied reference images. When references are supplied, preserve the real "
    "subject's appearance and important visual details."
)


def extract_post_plan_from_pdf(pdf_bytes: bytes) -> list[dict[str, str | None]]:
    """Extract one planned post per entry from an uploaded PDF planner."""
    extraction_prompt = """
Read the attached social media planner PDF and extract each distinct planned post.
Return only JSON in this shape:
{"posts": [{"topic": "post idea and useful details", "date": "YYYY-MM-DD or null", "start_time": "HH:MM or null"}]}

Rules:
- Include one entry for each planned post, including each day in a weekly plan.
- Preserve the plan's topic, offer, audience, and any useful instructions in topic.
- Convert dates to YYYY-MM-DD only when the actual date is clear in the document.
- Convert explicit times to 24-hour HH:MM; use null when date or time is not stated.
- Do not invent topics, dates, or times. Ignore unrelated document content.
""".strip()
    client = get_genai_client()
    response = client.models.generate_content(
        model=TEXT_MODEL,
        contents=[
            extraction_prompt,
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        ],
    )

    raw_response = (response.text or "").strip()
    if raw_response.startswith("```"):
        raw_response = raw_response.strip("`").removeprefix("json").strip()
    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as error:
        raise ValueError("Could not read a post plan from this PDF. Check that it contains a clear plan.") from error

    raw_posts = result.get("posts") if isinstance(result, dict) else None
    if not isinstance(raw_posts, list):
        raise ValueError("Could not read a post plan from this PDF. Check that it contains a clear plan.")

    posts = []
    for item in raw_posts:
        if not isinstance(item, dict) or not isinstance(item.get("topic"), str) or not item["topic"].strip():
            continue

        planned_date = item.get("date")
        if not isinstance(planned_date, str):
            planned_date = None
        else:
            try:
                planned_date = datetime.strptime(planned_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                planned_date = None

        start_time = item.get("start_time")
        if not isinstance(start_time, str):
            start_time = None
        else:
            try:
                start_time = datetime.strptime(start_time, "%H:%M").strftime("%H:%M")
            except ValueError:
                start_time = None

        posts.append({
            "topic": item["topic"].strip(),
            "date": planned_date,
            "start_time": start_time,
        })

    if not posts:
        raise ValueError("No post entries were found in this PDF planner.")
    return posts


def find_relevant_library_image(
    db: Session,
    prompt: str,
    company_id: int | None = None,
    min_threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict | None:
    """
    Embed the prompt text and find the closest-matching photo asset in the company's
    Library via cosine similarity against stored image embeddings.
    """
    try:
        prompt_embedding = generate_text_embedding(prompt)
    except Exception as err:
        logger.warning("Failed to generate prompt embedding, skipping library image search: %s", err)
        return None

    query = db.query(Library).filter(Library.media_type == "photo", Library.embedding.is_not(None))
    if company_id is not None:
        query = query.filter(Library.company_id == company_id)
    assets = query.all()
    if not assets:
        return None

    best_match = None
    best_score = -1.0
    for asset in assets:
        score = cosine_similarity(prompt_embedding, asset.embedding)
        if score > best_score:
            best_score = score
            best_match = asset

    if not best_match or best_score < min_threshold:
        return None

    return {"id": best_match.id, "name": best_match.name, "image_url": best_match.image_url, "similarity_score": best_score}


def generate_caption_and_hashtags(
    prompt: str,
    platform: str,
    tone: str | None = "Professional",
    language: str | None = "English (US)",
    extra_hashtags: list[str] | None = None,
    brand_context: str = "",
) -> dict:
    """Generate a headline, caption, hashtags, and safety score tailored to the target platform."""
    tone_str = tone or "Professional"
    lang_str = language or "English (US)"
    plat_str = platform.lower().strip()

    # Platform notes only set the format; the TONE line and the audience decide the voice.
    if plat_str == "instagram":
        platform_instructions = (
            "TARGET PLATFORM: Instagram\n"
            "FORMAT: Visual-first and easy to scan; use emojis only where they suit the tone.\n"
            "Write a 1-2 paragraph Instagram-optimized caption, then 8-12 relevant hashtags."
        )
    elif plat_str == "x":
        platform_instructions = (
            "TARGET PLATFORM: X (Twitter)\n"
            "FORMAT: Concise, under 260 characters total including hashtags.\n"
            "Write a short, attention-grabbing caption, then 2-4 relevant hashtags."
        )
    elif plat_str == "facebook":
        platform_instructions = (
            "TARGET PLATFORM: Facebook\n"
            "FORMAT: Easy to read in a feed and written to invite comments.\n"
            "Write a 1-2 paragraph caption, then 3-6 relevant hashtags."
        )
    else:
        platform_instructions = (
            "TARGET PLATFORM: LinkedIn\n"
            "FORMAT: Short, skimmable paragraphs with a clear takeaway.\n"
            "Write a 1-2 paragraph caption, then 5-8 relevant hashtags."
        )

    brand_block = f"\n{brand_context}\n" if brand_context else ""
    prompt_text = f"""
You are an expert social media content strategist writing on behalf of a business.

USER REQUEST / TOPIC:
{prompt}
{brand_block}
TONE: {tone_str}
LANGUAGE: {lang_str}

Write in the TONE above for the business's audience; the platform notes below only set the format.

{platform_instructions}

Return ONLY a JSON object (no markdown fences) with keys:
- "headline": a short, impactful headline (1 line) in {lang_str}
- "caption": the main body text of the post in {lang_str}
- "hashtags": hashtags separated by spaces
- "ai_safety_score": integer 0-100 evaluating content safety/appropriateness
"""

    default_result = {
        "headline": prompt[:80],
        "caption": prompt,
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
            "headline": data.get("headline") or default_result["headline"],
            "caption": data.get("caption") or default_result["caption"],
            "hashtags": hashtags,
            "ai_safety_score": score,
        }
    except Exception as err:
        logger.warning("Failed to generate caption via Gemini, using fallback: %s", err)
        return default_result


def generate_post_image(
    reference_images: list[dict],
    user_prompt: str,
    platform: str,
    brand_style: str | None = None,
    style_images: list[dict] | None = None,
) -> bytes | None:
    """
    Generate a new social post image from reference image bytes + the user prompt
    via Gemini's multimodal image model. Returns None if generation fails.

    brand_style is added on top of the platform's own style. style_images are the
    company's theme images, passed as style-only references (palette, mood, look),
    separate from the subject reference_images.
    """
    plat_str = platform.lower().strip()
    if plat_str == "instagram":
        style_guide = "Vibrant, modern lifestyle Instagram-style composition."
    else:
        style_guide = "Clean, high-impact, professional composition suitable for business social media."
    brand_style_block = f"\nBRAND STYLE (apply on top of the style guide above): {brand_style}\n" if brand_style else ""

    if reference_images and style_images:
        reference_guidance = "Use the image(s) labelled SUBJECT REFERENCE as the primary visual reference. Preserve the real subject's appearance and important details."
    elif reference_images:
        reference_guidance = "Use the supplied reference image(s) as the primary visual reference. Preserve the real subject's appearance and important details."
    else:
        reference_guidance = "Create an original image that visually represents the user's request. Do not invent a specific real brand logo or add text."
    style_reference_block = (
        "\nBRAND STYLE REFERENCES: The image(s) labelled BRAND STYLE REFERENCE come from the company's brand "
        "theme. Use them ONLY as style references: match their colour palette, mood, lighting and overall visual "
        "style. Never copy their content, subjects, layout, logos or any text from them.\n"
        if style_images
        else ""
    )
    combined_prompt = f"""
{DEFAULT_IMAGE_SYSTEM_PROMPT}

USER REQUEST:
{user_prompt}

STYLE GUIDE: {style_guide}
{brand_style_block}
VISUAL DIRECTION: {reference_guidance}
{style_reference_block}
CRITICAL: Do NOT render any text, titles, captions, or typography on the generated
image. Produce a clean, polished social media visual suitable for the target platform.
"""

    parts = [types.Part.from_text(text=combined_prompt)]
    for img in reference_images:
        if style_images:
            parts.append(types.Part.from_text(text="SUBJECT REFERENCE:"))
        parts.append(types.Part.from_bytes(data=img["bytes"], mime_type=img["mime_type"]))
    for img in style_images or []:
        parts.append(types.Part.from_text(text="BRAND STYLE REFERENCE (style only, do not copy its content):"))
        parts.append(types.Part.from_bytes(data=img["bytes"], mime_type=img["mime_type"]))

    try:
        client = get_genai_client()
        response = client.models.generate_content(
            model=IMAGE_MODEL,
            contents=[types.Content(role="user", parts=parts)],
        )
        for candidate in response.candidates or []:
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if part.inline_data and part.inline_data.data:
                        return part.inline_data.data
    except errors.ClientError as err:
        # A theme image the model rejects must not cost the post its image.
        if style_images:
            logger.warning("Gemini rejected the image request with brand style images, retrying without them: %s", err)
            return generate_post_image(reference_images, user_prompt, platform, brand_style)
        logger.warning("Failed to generate post image via Gemini: %s", err)
    except Exception as err:
        logger.warning("Failed to generate post image via Gemini: %s", err)

    return None


def create_generated_post(
    db: Session,
    company_id: int,
    prompt: str,
    platform: str,
    date: str | None = None,
    start_time: str | None = None,
    tone: str | None = None,
    language: str | None = "English (US)",
    hashtags: list[str] | None = None,
    custom_images_data: list[dict] | None = None,
    is_approved: bool = False,
    approved_at: datetime | None = None,
    generated_content: dict | None = None,
) -> GeneratedPost:
    """
    Full post generation workflow for one platform:
    1. Pick reference image(s): user-uploaded images take priority, otherwise search
       the shared Library via text-embedding similarity.
    2. Generate caption/headline/hashtags tailored to the platform.
    3. If a reference image is available, generate a new AI post image from it.
       (No reference image found -> caption-only draft; still valid for most platforms.)
    4. Stamp the brand logo on the generated image (if any) and upload it to Supabase storage.
    5. Save the draft as a GeneratedPost row scoped to the company.
    """
    brand = get_brand_profile(db, company_id)
    # The Themes screen sets the house tone; an explicit tone on the request still wins.
    tone = tone or (brand.brand_tone if brand else None) or "Professional"

    reference_id = None
    reference_url = None
    images_data: list[dict] = []

    if custom_images_data:
        for item in custom_images_data:
            images_data.append({"bytes": item["bytes"], "mime_type": item.get("mime_type", "image/png")})
        reference_id = custom_images_data[0].get("id")
        reference_url = custom_images_data[0].get("image_url")
    else:
        match = find_relevant_library_image(db, prompt, company_id)
        if match:
            reference_id = match["id"]
            reference_url = match["image_url"]
            try:
                res = requests.get(match["image_url"], timeout=30)
                res.raise_for_status()
                mime_type = "image/png" if match["image_url"].lower().endswith(".png") else "image/jpeg"
                images_data.append({"bytes": res.content, "mime_type": mime_type})
            except requests.RequestException as err:
                logger.warning("Failed to fetch matched library image bytes: %s", err)

    caption_data = generated_content or generate_caption_and_hashtags(
        prompt=prompt,
        platform=platform,
        tone=tone,
        language=language,
        extra_hashtags=hashtags,
        brand_context=brand_prompt_context(brand),
    )

    image_url = None
    generated_bytes = generate_post_image(
        images_data,
        prompt,
        platform,
        brand_style_guide(brand),
        load_brand_style_images(brand),
    )
    if generated_bytes:
        # The logo is stamped by code so it is always the real one, never an AI imitation.
        generated_bytes = apply_brand_logo(generated_bytes, brand)
        try:
            image_url = upload_library_asset(
                file_content=generated_bytes,
                filename=f"post_{platform}_{(reference_id or 'gen')[:8]}.png",
                content_type="image/png",
            )
        except Exception as err:
            logger.warning("Failed to upload generated post image, saving draft without image: %s", err)

    post = GeneratedPost(
        company_id=company_id,
        prompt=prompt,
        platform=platform,
        title=caption_data["headline"],
        headline=caption_data["headline"],
        caption=caption_data["caption"],
        hashtags=caption_data["hashtags"],
        image_url=image_url,
        reference_image_id=reference_id,
        reference_image_url=reference_url,
        date=date,
        start_time=start_time,
        tone=tone or "Professional",
        language=language or "English (US)",
        ai_safety_score=caption_data.get("ai_safety_score", 98),
        is_approved=is_approved,
        approved_at=approved_at,
        is_posted=False,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def publish_post_to_platform(post: GeneratedPost, credential: Credentials) -> dict:
    """
    Publish an already-generated draft post to its target platform using the
    company's stored OAuth credential. Raises RuntimeError with a user-facing
    message on any failure (missing auth, unsupported platform, API error).
    """
    full_text = (post.caption or "").strip()
    if post.hashtags and post.hashtags.strip():
        full_text = f"{full_text}\n\n{post.hashtags.strip()}"

    plat = (post.platform or "").lower().strip()

    if plat == "linkedin":
        if not credential.access_token:
            raise RuntimeError("LinkedIn account not connected. Authorize via POST /api/credentials/ first.")

        image_urn = None
        if post.image_url:
            try:
                img_bytes = requests.get(post.image_url, timeout=30).content
                if credential.organization_id:
                    clean_org = credential.organization_id.replace("urn:li:organization:", "").strip()
                    owner_urn = f"urn:li:organization:{clean_org}"
                else:
                    user_info = LinkedInService.get_user_info(credential.access_token)
                    owner_urn = f"urn:li:person:{user_info.get('sub')}"
                image_urn = LinkedInService.upload_image(
                    access_token=credential.access_token,
                    owner_urn=owner_urn,
                    file_bytes=img_bytes,
                    content_type="image/png",
                )
            except Exception as err:
                logger.warning("Failed to attach image to LinkedIn post, posting text only: %s", err)

        return LinkedInService.create_post(
            access_token=credential.access_token,
            text=full_text,
            image_urn=image_urn,
            organization_id=credential.organization_id,
        )

    if plat == "instagram":
        if not credential.access_token or not credential.organization_id:
            raise RuntimeError("Instagram account not connected. Authorize via POST /api/credentials/ first.")
        if not post.image_url:
            raise RuntimeError("Instagram requires an image; this draft has no generated image.")
        return InstagramService.create_post(
            access_token=credential.access_token,
            instagram_account_id=credential.organization_id,
            image_url=post.image_url,
            caption=full_text,
        )

    if plat == "facebook":
        if not credential.access_token or not credential.organization_id:
            raise RuntimeError("Facebook Page not connected. Authorize via POST /api/credentials/ first.")
        return FacebookService.create_post(
            page_access_token=credential.access_token,
            page_id=credential.organization_id,
            message=full_text,
            image_url=post.image_url,
        )

    if plat == "x":
        if not credential.access_token:
            raise RuntimeError("X (Twitter) account not connected. Authorize via POST /api/credentials/ first.")
        return TwitterService.create_post(access_token=credential.access_token, text=full_text)

    raise RuntimeError(f"Platform '{plat}' is not supported for publishing yet.")
