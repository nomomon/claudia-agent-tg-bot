"""Gemini text-to-speech, the same model the rat bot uses. Returns Ogg Opus for Telegram.

Long replies are split into chunks, the chunks are synthesized in parallel, and the raw PCM is
joined in order. One ffmpeg pass then encodes the result. Splitting keeps every request short,
which avoids the cut-offs we saw with long single requests.
"""

from __future__ import annotations

import asyncio
import re

from claudia_core import UserFacingError
from google import genai
from google.genai import errors, types

DEFAULT_MODEL = "gemini-2.5-pro-preview-tts"
DEFAULT_VOICE = "Achernar"
TONE = "Read aloud in a warm and friendly tone:\n"
CHUNK_CHARS = 700
MAX_PARALLEL = 4
DEFAULT_RATE = 24000
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraphs first, then sentences, so each chunk is at most about `size` chars."""
    pieces: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        if len(paragraph) <= size:
            pieces.append(paragraph)
        else:
            pieces.extend(s.strip() for s in _SENTENCE.split(paragraph) if s.strip())

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > size:
            chunks.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        chunks.append(current)
    return chunks


def _sample_rate(mime_type: str | None) -> int:
    """Read rate=24000 from a mime type like 'audio/L16;codec=pcm;rate=24000'."""
    for param in (mime_type or "").split(";"):
        key, _, value = param.strip().partition("=")
        if key == "rate" and value.isdigit():
            return int(value)
    return DEFAULT_RATE


def _speech_error(exc: errors.APIError) -> UserFacingError | None:
    if exc.code == 429:
        return UserFacingError("voice_quota")
    if exc.code in (401, 403):
        return UserFacingError("voice_key_invalid")
    return None


class GeminiSpeech:
    def __init__(
        self,
        client: genai.Client,
        *,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
    ) -> None:
        self._client = client
        self._model = model
        self._voice = voice

    async def synthesize(self, text: str) -> bytes:
        chunks = chunk_text(text)
        if not chunks:
            raise UserFacingError("voice_failed")

        gate = asyncio.Semaphore(MAX_PARALLEL)

        async def one(chunk: str) -> tuple[bytes, int]:
            async with gate:
                return await self._pcm(chunk)

        # gather keeps input order, so the audio plays back in the order the text was written.
        parts = await asyncio.gather(*(one(chunk) for chunk in chunks))
        pcm = b"".join(data for data, _ in parts)
        return await _encode_ogg(pcm, parts[0][1])

    async def _pcm(self, chunk: str) -> tuple[bytes, int]:
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
            ),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=TONE + chunk,
                config=config,
            )
        except errors.APIError as exc:
            mapped = _speech_error(exc)
            if mapped is None:
                raise
            raise mapped from exc

        candidates = response.candidates or []
        content = candidates[0].content if candidates else None
        part = content.parts[0] if content and content.parts else None
        if part is None or part.inline_data is None or not part.inline_data.data:
            raise UserFacingError("voice_failed")
        return part.inline_data.data, _sample_rate(part.inline_data.mime_type)


async def _encode_ogg(pcm: bytes, rate: int) -> bytes:
    """Raw 16-bit mono PCM to Ogg Opus, through ffmpeg on stdin and stdout."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        "64k",
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(pcm)
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {err.decode(errors='replace')}")
    return out
