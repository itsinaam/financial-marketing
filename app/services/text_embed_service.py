import math
from google.genai import types
from app.services.image_embed_service import get_genai_client


def generate_text_embedding(text: str) -> list[float]:
    """
    Generate Gemini embedding vector (768-dim) for a text string.
    Uses the same 'gemini-embedding-2' model/dimensionality as image embeddings
    so text queries and image embeddings share one comparable vector space.
    """
    if not text or not text.strip():
        raise ValueError("Text cannot be empty.")

    client = get_genai_client()
    result = client.models.embed_content(
        model="gemini-embedding-2",
        contents=text.strip(),
        config=types.EmbedContentConfig(output_dimensionality=768),
    )

    if not result.embeddings:
        raise RuntimeError("Gemini API returned an empty response for text embedding.")

    return list(result.embeddings[0].values)


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity score between two float vectors."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)
