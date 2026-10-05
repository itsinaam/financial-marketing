from typing import Any

from fastapi import APIRouter, Depends

from app.core import deps
from app.models.companies import Company
from app.services.speech_to_text_service import create_gemini_transcription_session

router = APIRouter()

@router.post("/transcribe", summary="Create a Gemini realtime transcription session")
def create_transcription_session(
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    return create_gemini_transcription_session()