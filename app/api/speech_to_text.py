from mimetypes import guess_type
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.core import deps
from app.models.companies import Company
from app.services.speech_to_text_service import transcribe_audio

router = APIRouter()

MAX_AUDIO_BYTES = 4 * 1024 * 1024
SUPPORTED_AUDIO_EXTENSIONS = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".wav", ".webm"}


@router.post("/transcribe", summary="Transcribe a short audio clip")
def transcribe_speech(
    file: UploadFile = File(..., description="Audio file; WebM from MediaRecorder is supported"),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    filename = file.filename or ""
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported audio format. Supported extensions: {', '.join(sorted(SUPPORTED_AUDIO_EXTENSIONS))}.",
        )

    audio_bytes = file.file.read(MAX_AUDIO_BYTES + 1)
    if not audio_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The audio file is empty.",
        )
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Audio file exceeds the 4 MiB limit. Send shorter clips.",
        )

    content_type = (file.content_type or guess_type(filename)[0] or "application/octet-stream")
    content_type = content_type.split(";", 1)[0]
    text = transcribe_audio(
        audio_bytes=audio_bytes,
        filename=filename,
        content_type=content_type,
    )
    return {"text": text}