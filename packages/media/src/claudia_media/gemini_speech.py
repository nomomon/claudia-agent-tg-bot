"""Gemini text-to-speech, the same model the rat bot uses. Streams Ogg Opus segments.

A reply is cut into segments at sentence boundaries. The first segment can be longer than the
rest, so the first voice note covers about a minute of speech. Segments are synthesized a few at
a time, ahead of playback, and yielded in order. Each segment is encoded on its own, so every one
is a complete voice note.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass

from claudia_core import SpeechPiece, UserFacingError
from google import genai
from google.genai import errors, types

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-pro-preview-tts"
DEFAULT_VOICE = "Achernar"
TONE = "Read aloud in a warm and friendly tone:\n"
DEFAULT_RATE = 24000
RETRIES = 2
RETRY_DELAY_SECONDS = 1.0

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class SegmentPlan:
    """Sizes in characters. Roughly 15 characters are spoken per second."""

    first_chars: int = 900  # about one minute of speech in the first voice note
    rest_chars: int = 900
    window: int = 2  # how many segments are synthesized ahead of playback


def plan_segments(text: str, first_chars: int, rest_chars: int) -> list[str]:
    """Cut text at sentence boundaries into segments that fit the sizes given."""
    sentences: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if paragraph:
            sentences.extend(s.strip() for s in _SENTENCE.split(paragraph) if s.strip())

    segments: list[str] = []
    current = ""
    limit = first_chars
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > limit:
            segments.append(current)
            current = ""
            limit = rest_chars
        current = f"{current} {sentence}".strip()
    if current:
        segments.append(current)
    return segments


def _sample_rate(mime_type: str | None) -> int:
    """Read rate=24000 from a mime type like 'audio/L16;codec=pcm;rate=24000'."""
    for param in (mime_type or "").split(";"):
        key, _, value = param.strip().partition("=")
        if key == "rate" and value.isdigit():
            return int(value)
    return DEFAULT_RATE


def _quota_or_key_error(exc: errors.APIError) -> str | None:
    if exc.code == 429:
        return "voice_quota"
    if exc.code in (401, 403):
        return "voice_key_invalid"
    return None


class GeminiSpeech:
    def __init__(
        self,
        client: genai.Client,
        *,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        plan: SegmentPlan | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._voice = voice
        self._plan = plan or SegmentPlan()

    async def stream(self, text: str) -> AsyncGenerator[SpeechPiece, None]:
        """Yield one SpeechPiece per segment, in order. A failed segment yields an error piece.

        Closing the iterator cancels any segment still being made, so an interrupted reply
        stops spending speech requests.
        """
        segments = plan_segments(text, self._plan.first_chars, self._plan.rest_chars)
        pending: deque[asyncio.Task[SpeechPiece]] = deque()
        next_index = 0
        try:
            while next_index < len(segments) and len(pending) < self._plan.window:
                pending.append(asyncio.create_task(self._segment(segments[next_index])))
                next_index += 1
            while pending:
                piece = await pending.popleft()
                if next_index < len(segments):
                    pending.append(asyncio.create_task(self._segment(segments[next_index])))
                    next_index += 1
                yield piece
        finally:
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

    async def _segment(self, text: str) -> SpeechPiece:
        """Make one segment as an Ogg Opus voice note. Retries transient failures."""
        for attempt in range(RETRIES + 1):
            try:
                pcm, rate = await self._pcm(text)
                return SpeechPiece(text=text, audio=await _encode_ogg(pcm, rate))
            except UserFacingError as exc:
                # Quota, a bad key and empty audio will not fix themselves on a retry.
                return SpeechPiece(text=text, error=exc.key)
            except errors.APIError as exc:
                if exc.code < 500:
                    log.warning("speech request refused: %s", exc)
                    return SpeechPiece(text=text, error="voice_failed")
                log.warning("speech attempt %d failed: %s", attempt + 1, exc)
            except Exception as exc:
                log.warning("speech attempt %d failed: %s", attempt + 1, exc)
            if attempt < RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
        return SpeechPiece(text=text, error="voice_failed")

    async def _pcm(self, text: str) -> tuple[bytes, int]:
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
                contents=TONE + text,
                config=config,
            )
        except errors.APIError as exc:
            mapped = _quota_or_key_error(exc)
            if mapped is not None:
                raise UserFacingError(mapped) from exc
            raise

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
