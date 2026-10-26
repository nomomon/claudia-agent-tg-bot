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
# A 200-character segment takes about 15s. Longer requests hung past 4 minutes in tests.
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_PARALLEL_REQUESTS = 3

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class SegmentPlan:
    """Sizes in characters. Roughly 15 characters are spoken per second.

    Chosen from docs/voice-segments.md: short segments return in about the length of their
    audio. The first voice note is one segment, so it comes quickly. Later voice notes join
    per_bubble segments, so a long reply is a few notes rather than one per segment.
    """

    first_chars: int = 200  # about 15 seconds of speech, so the first voice note comes quickly
    rest_chars: int = 250
    per_bubble: int = 3  # segments joined into one voice note, about 50 seconds of speech
    window: int = 2  # how many voice notes are synthesized ahead of playback


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


def plan_bubbles(text: str, plan: SegmentPlan) -> list[list[str]]:
    """Group segments into voice notes. The first note is one segment, the rest per_bubble."""
    segments = plan_segments(text, plan.first_chars, plan.rest_chars)
    if not segments:
        return []
    rest = segments[1:]
    return [segments[:1]] + [
        rest[i : i + plan.per_bubble] for i in range(0, len(rest), plan.per_bubble)
    ]


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
        self._slots = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)

    async def stream(self, text: str) -> AsyncGenerator[SpeechPiece, None]:
        """Yield one SpeechPiece per voice note, in order. A failed note yields an error piece.

        Closing the iterator cancels any note still being made, so an interrupted reply
        stops spending speech requests.
        """
        bubbles = plan_bubbles(text, self._plan)
        pending: deque[asyncio.Task[SpeechPiece]] = deque()
        next_index = 0
        try:
            while next_index < len(bubbles) and len(pending) < self._plan.window:
                pending.append(asyncio.create_task(self._bubble(bubbles[next_index])))
                next_index += 1
            while pending:
                piece = await pending.popleft()
                if next_index < len(bubbles):
                    pending.append(asyncio.create_task(self._bubble(bubbles[next_index])))
                    next_index += 1
                yield piece
        finally:
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

    async def _bubble(self, segments: list[str]) -> SpeechPiece:
        """Make the segments in parallel and join their audio into one Ogg Opus voice note."""
        text = " ".join(segments)
        made = await asyncio.gather(*(self._make(segment) for segment in segments))
        audio: list[bytes] = []
        rate = DEFAULT_RATE
        for result in made:
            if isinstance(result, str):
                return SpeechPiece(text=text, error=result)
            audio.append(result[0])
            rate = result[1]
        return SpeechPiece(text=text, audio=await _encode_ogg(b"".join(audio), rate))

    async def _make(self, text: str) -> tuple[bytes, int] | str:
        """Raw audio for one segment, or the message key for why it failed. Retries transient
        errors."""
        for attempt in range(RETRIES + 1):
            try:
                return await self._pcm(text)
            except UserFacingError as exc:
                # Quota, a bad key and empty audio will not fix themselves on a retry.
                return exc.key
            except errors.APIError as exc:
                if exc.code < 500:
                    log.warning("speech request refused: %s", exc)
                    return "voice_failed"
                log.warning("speech attempt %d failed: %s", attempt + 1, exc)
            except Exception as exc:
                log.warning("speech attempt %d failed: %s", attempt + 1, exc)
            if attempt < RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
        return "voice_failed"

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
            async with self._slots:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=self._model,
                        contents=TONE + text,
                        config=config,
                    ),
                    timeout=REQUEST_TIMEOUT_SECONDS,
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
