import asyncio
import shutil
from types import SimpleNamespace
from typing import Any, cast

import pytest
from claudia_media import gemini_speech
from claudia_media.gemini_speech import (
    GeminiSpeech,
    SegmentPlan,
    _sample_rate,
    plan_segments,
)
from google import genai
from google.genai import errors

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def test_first_segment_is_the_bigger_one() -> None:
    text = " ".join(f"Это предложение номер {i}." for i in range(400))
    segments = plan_segments(text, first_chars=900, rest_chars=300)
    assert len(segments[0]) <= 900 + 40
    assert all(len(s) <= 300 + 40 for s in segments[1:])
    assert " ".join(segments).split() == text.split()


def test_default_segments_stay_short() -> None:
    plan = SegmentPlan()
    text = " ".join(f"Это предложение номер {i}, оно короткое." for i in range(200))
    segments = plan_segments(text, plan.first_chars, plan.rest_chars)
    assert len(segments[0]) <= plan.first_chars + 40
    assert all(len(s) <= plan.rest_chars + 40 for s in segments[1:])


def test_segments_break_at_sentences() -> None:
    segments = plan_segments("Первое. Второе! Третье? Четвёртое.", 12, 12)
    assert all(s.endswith((".", "!", "?")) for s in segments)


def test_short_text_is_one_segment_and_blank_is_none() -> None:
    assert plan_segments("Привет!", 900, 900) == ["Привет!"]
    assert plan_segments("   ", 900, 900) == []


def test_sample_rate_is_read_from_mime_type() -> None:
    assert _sample_rate("audio/L16;codec=pcm;rate=16000") == 16000
    assert _sample_rate(None) == 24000


class _FakeModels:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def generate_content(self, **kwargs: Any) -> Any:
        outcome = self._outcome(kwargs["contents"]) if callable(self._outcome) else self._outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _audio_response(pcm: bytes) -> Any:
    part = SimpleNamespace(
        inline_data=SimpleNamespace(data=pcm, mime_type="audio/L16;codec=pcm;rate=24000")
    )
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])


def _client(outcome: Any) -> genai.Client:
    return cast(genai.Client, SimpleNamespace(aio=SimpleNamespace(models=_FakeModels(outcome))))


SILENCE = b"\x00\x00" * 24000  # one second of 24 kHz mono silence


@requires_ffmpeg
async def test_stream_yields_ogg_segments_in_order_with_their_text() -> None:
    speech = GeminiSpeech(_client(_audio_response(SILENCE)), plan=SegmentPlan(20, 20, 2))
    pieces = [p async for p in speech.stream("Первое предложение. Второе предложение. Третье.")]
    assert len(pieces) >= 2
    assert all(p.audio is not None and p.audio[:4] == b"OggS" for p in pieces)
    assert " ".join(p.text for p in pieces).split() == (
        "Первое предложение. Второе предложение. Третье.".split()
    )


async def test_quota_error_is_reported_on_its_piece_and_not_retried() -> None:
    calls = 0

    def refuse(_: str) -> Any:
        nonlocal calls
        calls += 1
        return errors.APIError(429, {"error": {"message": "quota"}})

    speech = GeminiSpeech(_client(refuse))
    pieces = [p async for p in speech.stream("Привет")]
    assert [p.error for p in pieces] == ["voice_quota"]
    assert calls == 1


async def test_transient_server_error_is_retried_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("claudia_media.gemini_speech.RETRY_DELAY_SECONDS", 0)
    attempts = 0

    def flaky(_: str) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return errors.APIError(503, {"error": {"message": "busy"}})
        return _audio_response(SILENCE)

    speech = GeminiSpeech(_client(flaky))
    pieces = [p async for p in speech.stream("Привет")]
    assert attempts == 2
    assert pieces[0].error is None and pieces[0].audio is not None


async def test_persistent_failure_becomes_an_error_piece(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("claudia_media.gemini_speech.RETRY_DELAY_SECONDS", 0)
    speech = GeminiSpeech(_client(errors.APIError(503, {"error": {"message": "busy"}})))
    pieces = [p async for p in speech.stream("Привет")]
    assert [p.error for p in pieces] == ["voice_failed"]


async def test_empty_audio_is_an_error_piece() -> None:
    speech = GeminiSpeech(_client(SimpleNamespace(candidates=[])))
    pieces = [p async for p in speech.stream("Привет")]
    assert [p.error for p in pieces] == ["voice_failed"]


async def test_closing_the_stream_stops_pending_work() -> None:
    speech = GeminiSpeech(_client(_audio_response(SILENCE)), plan=SegmentPlan(5, 5, 2))
    stream = speech.stream("Раз. Два. Три. Четыре. Пять. Шесть.")
    await anext(stream)
    await stream.aclose()  # must not hang or leak tasks


async def test_hung_request_times_out_and_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Hanging:
        async def generate_content(self, **kwargs: Any) -> Any:
            await asyncio.sleep(10)

    client = cast(genai.Client, SimpleNamespace(aio=SimpleNamespace(models=_Hanging())))
    monkeypatch.setattr(gemini_speech, "REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(gemini_speech, "RETRY_DELAY_SECONDS", 0)
    pieces = [p async for p in GeminiSpeech(client, plan=SegmentPlan(20, 20, 1)).stream("Привет.")]
    assert [p.error for p in pieces] == ["voice_failed"]
