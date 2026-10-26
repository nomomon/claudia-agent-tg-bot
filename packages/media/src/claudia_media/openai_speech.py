from __future__ import annotations

from claudia_core import UserFacingError
from openai import APIError, AsyncOpenAI


class OpenAITranscriber:
    """Speech to text with OpenAI Whisper."""

    def __init__(self, client: AsyncOpenAI, *, stt_model: str = "whisper-1") -> None:
        self._client = client
        self._stt_model = stt_model

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        extension = mime_type.split("/")[-1].split(";")[0] or "ogg"
        try:
            result = await self._client.audio.transcriptions.create(
                model=self._stt_model,
                file=(f"input.{extension}", audio, mime_type),
            )
        except APIError as exc:
            raise _speech_error(exc) or exc from exc
        return result.text.strip()


def _speech_error(exc: APIError) -> UserFacingError | None:
    """Translate known OpenAI failures into message keys. Other errors are re-raised."""
    if getattr(exc, "code", None) == "insufficient_quota":
        return UserFacingError("voice_quota")
    if getattr(exc, "status_code", None) == 401:
        return UserFacingError("voice_key_invalid")
    return None
