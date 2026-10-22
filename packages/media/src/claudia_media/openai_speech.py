from __future__ import annotations

from openai import AsyncOpenAI


class OpenAISpeech:
    """Whisper for transcription, OpenAI TTS for replies.

    Opus output is an Ogg container, which is what Telegram voice notes expect,
    so no ffmpeg step is needed.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        stt_model: str = "whisper-1",
        tts_model: str = "gpt-4o-mini-tts",
        tts_voice: str = "alloy",
    ) -> None:
        self._client = client
        self._stt_model = stt_model
        self._tts_model = tts_model
        self._tts_voice = tts_voice

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        extension = mime_type.split("/")[-1].split(";")[0] or "ogg"
        result = await self._client.audio.transcriptions.create(
            model=self._stt_model,
            file=(f"input.{extension}", audio, mime_type),
        )
        return result.text.strip()

    async def synthesize(self, text: str) -> bytes:
        response = await self._client.audio.speech.create(
            model=self._tts_model,
            voice=self._tts_voice,
            input=text,
            response_format="opus",
        )
        return response.content
