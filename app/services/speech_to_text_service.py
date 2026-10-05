from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from google import genai

from app.core.config import settings

GEMINI_LIVE_TRANSCRIPTION_MODEL = "gemini-3.5-transcribe-live"


def create_gemini_transcription_session() -> dict:
    if not settings.GEMINI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini speech transcription is not configured.",
        )

    try:
        now = datetime.now(timezone.utc)
        setup = {
            "response_modalities": ["TEXT"],
            "input_audio_transcription": {"language_codes": []},
        }
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        try:
            token = client.auth_tokens.create(
                config={
                    "uses": 1,
                    "expire_time": now + timedelta(minutes=30),
                    "new_session_expire_time": now + timedelta(minutes=1),
                    "live_connect_constraints": {
                        "model": GEMINI_LIVE_TRANSCRIPTION_MODEL,
                        "config": setup,
                    },
                },
            )
        finally:
            client.close()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not create a Gemini realtime transcription session.",
        ) from exc

    return {
        "token": token.name,
        "model": f"models/{GEMINI_LIVE_TRANSCRIPTION_MODEL}",
        "setup": {
            "responseModalities": ["TEXT"],
            "inputAudioTranscription": {"languageCodes": []},
        },
    }
