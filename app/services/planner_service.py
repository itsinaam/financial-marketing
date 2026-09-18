import json
import logging

from app.services.image_embed_service import get_genai_client

logger = logging.getLogger("PlannerService")

TEXT_MODEL = "gemini-3.6-flash"


def generate_content_plan(
    count: int,
    platforms: list[str],
    language: str = "English (US)",
    topic: str | None = None,
    company_description: str | None = None,
    brand_tone: str | None = None,
    target_audience: str | None = None,
) -> list[dict]:
    """
    Plan a run of posts in a single model call: every slot comes back with its own
    headline, caption and hashtags, so a whole week or month costs one request
    instead of one per post.

    Returns a list of dicts (headline, caption, hashtags, ai_safety_score). The list
    is padded with simple placeholders if the model returns fewer entries than asked.
    """
    tone = brand_tone or "Professional"
    audience = target_audience or "a general business audience"
    brand_line = f"\nABOUT THE BUSINESS:\n{company_description}\n" if company_description else ""
    topic_line = f"\nTHEME FOR THIS PLAN:\n{topic}\n" if topic else ""

    prompt_text = f"""
You are a social media strategist planning a content calendar for a business.
{brand_line}{topic_line}
Plan exactly {count} DISTINCT posts that work as a cohesive series: vary the angle of
each one (educational, insight, story, tip, question, announcement) so the set does not
repeat itself. They will be published across these platforms: {', '.join(platforms)}.

TONE: {tone}
AUDIENCE: {audience}
LANGUAGE: {language}

Return ONLY a JSON array (no markdown fences) of exactly {count} objects, each with keys:
- "headline": a short, impactful headline (1 line) in {language}
- "caption": the post body in {language}, 1-2 short paragraphs
- "hashtags": 3-8 relevant hashtags separated by spaces
- "ai_safety_score": integer 0-100 evaluating content safety/appropriateness
"""

    plan: list[dict] = []
    try:
        client = get_genai_client()
        response = client.models.generate_content(model=TEXT_MODEL, contents=prompt_text)
        raw = (response.text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("posts") or data.get("items") or []

        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                score = int(entry.get("ai_safety_score", 98))
            except (ValueError, TypeError):
                score = 98
            plan.append(
                {
                    "headline": (entry.get("headline") or "").strip() or None,
                    "caption": (entry.get("caption") or "").strip() or None,
                    "hashtags": (entry.get("hashtags") or "").strip(),
                    "ai_safety_score": score,
                }
            )
    except Exception as err:
        logger.warning("Failed to generate content plan via Gemini, falling back to placeholders: %s", err)

    fallback_topic = topic or company_description or "your business"
    while len(plan) < count:
        plan.append(
            {
                "headline": f"Content idea {len(plan) + 1} for {fallback_topic}"[:200],
                "caption": f"Draft this post about {fallback_topic}.",
                "hashtags": "",
                "ai_safety_score": 98,
            }
        )

    return plan[:count]
