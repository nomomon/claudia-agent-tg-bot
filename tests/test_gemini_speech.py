import shutil
from types import SimpleNamespace
from typing import Any, cast

import pytest
from claudia_core import UserFacingError
from claudia_media.gemini_speech import CHUNK_CHARS, GeminiSpeech, _sample_rate, chunk_text
from google import genai
from google.genai import errors


def test_chunks_respect_size_and_keep_all_words() -> None:
    text = ". ".join(f"Предложение номер {i} для проверки" for i in range(300)) + "."
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= CHUNK_CHARS + 60 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("Привет!") == ["Привет!"]
    assert chunk_text("   ") == []


def test_sample_rate_is_read_from_mime_type() -> None:
    assert _sample_rate("audio/L16;codec=pcm;rate=16000") == 16000
    assert _sample_rate(None) == 24000


class _FakeModels:
    def __init__(self, outcome) -> None:
        self._outcome = outcome

    async def generate_content(self, **kwargs):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _audio_response(pcm: bytes):
    part = SimpleNamespace(
        inline_data=SimpleNamespace(data=pcm, mime_type="audio/L16;codec=pcm;rate=24000")
    )
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])


def _client(outcome: Any) -> genai.Client:
    return cast(genai.Client, SimpleNamespace(aio=SimpleNamespace(models=_FakeModels(outcome))))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required to encode")
async def test_synthesize_returns_ogg_opus() -> None:
    silence = b"\x00\x00" * 24000  # one second of 24 kHz mono silence
    speech = GeminiSpeech(_client(_audio_response(silence)))
    ogg = await speech.synthesize("Привет, как дела?")
    assert ogg[:4] == b"OggS"


async def test_quota_error_becomes_a_message_key() -> None:
    exc = errors.APIError(429, {"error": {"message": "quota"}})
    speech = GeminiSpeech(_client(exc))
    with pytest.raises(UserFacingError) as caught:
        await speech.synthesize("Привет")
    assert caught.value.key == "voice_quota"


async def test_empty_audio_is_reported_as_failed_voice() -> None:
    speech = GeminiSpeech(_client(SimpleNamespace(candidates=[])))
    with pytest.raises(UserFacingError) as caught:
        await speech.synthesize("Привет")
    assert caught.value.key == "voice_failed"
