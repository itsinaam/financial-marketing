import json
import logging
import requests
from google.genai import types
from sqlalchemy.orm import Session

from app.models.credentials import Credentials
from app.models.library import Library
from app.models.post import GeneratedPost
from app.services.image_embed_service import get_genai_client
from app.services.text_embed_service import generate_text_embedding, cosine_similarity
from app.services.storage_service import upload_library_asset
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
    "professional social media post image based on the reference image(s) and the "
    "user's request. Preserve the real subject's appearance and important visual "
    "details rather than inventing a different product."
)


def find_relevant_library_image(db: Session, prompt: str, min_threshold: float = DEFAULT_MATCH_THRESHOLD) -> dict | None:
    """
    Embed the prompt text and find the closest-matching photo asset in the shared
    Library via cosine similarity against stored image embeddings.
    """
    try:
        prompt_embedding = generate_text_embedding(prompt)
    except Exception as err:
        logger.warning("Failed to generate prompt embedding, skipping library image search: %s", err)
        return None

    assets = (
        db.query(Library)
        .filter(Library.media_type == "photo", Library.embedding.is_not(None))
        .all()
    )
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
) -> dict:
    """Generate a headline, caption, hashtags, and safety score tailored to the target platform."""
    tone_str = tone or "Professional"
    lang_str = language or "English (US)"
    plat_str = platform.lower().strip()

    if plat_str == "instagram":
        platform_instructions = (
            "TARGET PLATFORM: Instagram\n"
            "STYLE: Casual, vibrant, lifestyle-oriented with natural emojis.\n"
            "Write a 1-2 paragraph Instagram-optimized caption, then 8-12 relevant hashtags."
        )
    elif plat_str == "x":
        platform_instructions = (
            "TARGET PLATFORM: X (Twitter)\n"
            "STYLE: Punchy and concise, under 260 characters total including hashtags.\n"
            "Write a short, attention-grabbing caption, then 2-4 relevant hashtags."
        )
    elif plat_str == "facebook":
        platform_instructions = (
            "TARGET PLATFORM: Facebook\n"
            "STYLE: Friendly, conversational, community-oriented.\n"
            "Write a 1-2 paragraph caption, then 3-6 relevant hashtags."
        )
    else:
        platform_instructions = (
            "TARGET PLATFORM: LinkedIn\n"
            f"STYLE: Professional, corporate, thought-leadership tone matching '{tone_str}'.\n"
            "Write a 1-2 paragraph B2B-appropriate caption, then 5-8 relevant corporate hashtags."
        )

    prompt_text = f"""
You are an expert social media content strategist writing on behalf of a business.

USER REQUEST / TOPIC:
{prompt}

TONE: {tone_str}
LANGUAGE: {lang_str}

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


def generate_post_image(reference_images: list[dict], user_prompt: str, platform: str) -> bytes | None:
    """
    Generate a new social post image from reference image bytes + the user prompt
    via Gemini's multimodal image model. Returns None if generation fails.
    """
    if not reference_images:
        return None

    plat_str = platform.lower().strip()
    if plat_str == "instagram":
        style_guide = "Vibrant, modern lifestyle Instagram-style composition."
    else:
        style_guide = "Clean, high-impact, professional composition suitable for business social media."

    combined_prompt = f"""
{DEFAULT_IMAGE_SYSTEM_PROMPT}

USER REQUEST:
{user_prompt}

STYLE GUIDE: {style_guide}

CRITICAL: Do NOT render any text, titles, captions, or typography on the generated
image. Produce clean photography only, using the supplied reference image(s) as the
primary visual reference. Do not replace the real subject with a fictional product.
"""

    parts = [types.Part.from_text(text=combined_prompt)]
    for img in reference_images:
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
    tone: str | None = "Professional",
    language: str | None = "English (US)",
    hashtags: list[str] | None = None,
    custom_images_data: list[dict] | None = None,
) -> GeneratedPost:
    """
    Full post generation workflow for one platform:
    1. Pick reference image(s): user-uploaded images take priority, otherwise search
       the shared Library via text-embedding similarity.
    2. Generate caption/headline/hashtags tailored to the platform.
    3. If a reference image is available, generate a new AI post image from it.
       (No reference image found -> caption-only draft; still valid for most platforms.)
    4. Upload the generated image (if any) to Supabase storage.
    5. Save the draft as a GeneratedPost row scoped to the company.
    """
    reference_id = None
    reference_url = None
    images_data: list[dict] = []

    if custom_images_data:
        for item in custom_images_data:
            images_data.append({"bytes": item["bytes"], "mime_type": item.get("mime_type", "image/png")})
        reference_id = custom_images_data[0].get("id")
        reference_url = custom_images_data[0].get("image_url")
    else:
        match = find_relevant_library_image(db, prompt)
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

    caption_data = generate_caption_and_hashtags(
        prompt=prompt,
        platform=platform,
        tone=tone,
        language=language,
        extra_hashtags=hashtags,
    )

    image_url = None
    generated_bytes = generate_post_image(images_data, prompt, platform)
    if generated_bytes:
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
        is_approved=False,
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
