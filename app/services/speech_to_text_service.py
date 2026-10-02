from fastapi import HTTPException, status
from openai import OpenAI, OpenAIError

from app.core.config import settings


def transcribe_audio(
    audio_bytes: bytes,
    filename: str,
    content_type: str,
) -> str:
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Speech transcription is not configured.",
        )

    request_data = {
        "model": "gpt-4o-mini-transcribe",
        "file": (filename, audio_bytes, content_type),
    }

    try:
        result = OpenAI(api_key=settings.OPENAI_API_KEY).audio.transcriptions.create(
            **request_data
        )
    except OpenAIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Speech transcription provider request failed.",
        ) from exc

    return result.text.strip()