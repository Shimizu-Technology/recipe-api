"""Text-to-Speech router using OpenAI TTS API."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Literal
import httpx

from app.config import get_settings

router = APIRouter(prefix="/api/tts", tags=["TTS"])
settings = get_settings()

# Available OpenAI TTS voices
TTS_VOICES = Literal["alloy", "echo", "fable", "onyx", "nova", "shimmer"]


class TTSRequest(BaseModel):
    """Request body for TTS generation."""
    text: str
    voice: TTS_VOICES = "nova"  # Default to nova (warm, natural)


@router.post("")
async def generate_tts(request: TTSRequest):
    """
    Generate speech from text using OpenAI TTS API.
    
    Returns an audio stream (MP3 format).
    
    Voices:
    - alloy: Neutral, balanced
    - echo: Soft, gentle
    - fable: Expressive, storytelling
    - onyx: Deep, authoritative
    - nova: Warm, natural (default)
    - shimmer: Clear, bright
    """
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Text is required")
    
    # Limit text length to prevent abuse (approx 4096 chars = ~1 min audio)
    if len(request.text) > 4096:
        raise HTTPException(
            status_code=400, 
            detail="Text too long. Maximum 4096 characters."
        )
    
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=500,
            detail="OpenAI API key not configured"
        )
    
    try:
        # Call OpenAI TTS API
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "tts-1",  # Use tts-1 for speed, tts-1-hd for quality
                    "input": request.text,
                    "voice": request.voice,
                    "response_format": "mp3",
                },
            )
            
            if response.status_code != 200:
                error_detail = response.text[:200] if response.text else "Unknown error"
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"OpenAI TTS API error: {error_detail}"
                )
            
            # Return audio as streaming response
            return StreamingResponse(
                iter([response.content]),
                media_type="audio/mpeg",
                headers={
                    "Content-Disposition": "inline; filename=speech.mp3",
                    "Cache-Control": "no-cache",
                }
            )
            
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="TTS generation timed out. Try shorter text."
        )
    except Exception as e:
        print(f"❌ TTS error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"TTS generation failed: {str(e)}"
        )


@router.get("/voices")
async def list_voices():
    """List available TTS voices with descriptions."""
    return {
        "voices": [
            {"id": "alloy", "name": "Alloy", "description": "Neutral, balanced"},
            {"id": "echo", "name": "Echo", "description": "Soft, gentle"},
            {"id": "fable", "name": "Fable", "description": "Expressive, storytelling"},
            {"id": "onyx", "name": "Onyx", "description": "Deep, authoritative"},
            {"id": "nova", "name": "Nova", "description": "Warm, natural"},
            {"id": "shimmer", "name": "Shimmer", "description": "Clear, bright"},
        ],
        "default": "nova"
    }
