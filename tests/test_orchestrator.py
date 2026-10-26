import asyncio

from claudia_channels import Orchestrator
from claudia_core import Attachment, InboundMessage, Principal, Role, SpeechPiece
from claudia_harness import HarnessRegistry, Outbox, TurnResult
from claudia_store import SqliteStore

from tests.fakes import Call, FakeHarness, FakeSink, FakeSpeaker, FakeTranscriber


def _orchestrator(
    store: SqliteStore,
    harness: FakeHarness,
    *,
    transcript: str | None = None,
) -> Orchestrator:
    return Orchestrator(
        repo=store,
        harnesses=HarnessRegistry({"claude": harness}),
        transcriber=FakeTranscriber(transcript) if transcript is not None else None,
        speaker=FakeSpeaker(),
    )


def _text(sender: str, text: str) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id=sender, text=text)


def _voice(sender: str) -> InboundMessage:
    audio = Attachment("audio", b"raw-audio", "audio/ogg")
    return InboundMessage(channel="telegram", sender_id=sender, attachments=(audio,))


async def test_unknown_sender_gets_no_reply(seeded: SqliteStore) -> None:
    harness, sink = FakeHarness(), FakeSink()
    await _orchestrator(seeded, harness).handle(_text("999", "hello"), sink)
    assert sink.sent == []
    assert harness.calls == []


async def test_each_person_gets_their_own_session(seeded: SqliteStore) -> None:
    harness = FakeHarness()
    orchestrator = _orchestrator(seeded, harness)

    await orchestrator.handle(_text("111", "admin here"), FakeSink())
    await orchestrator.handle(_text("222", "sister here"), FakeSink())
    await orchestrator.handle(_text("111", "admin again"), FakeSink())

    admin_calls = [c for c in harness.calls if c.profile.name == "admin"]
    sister_calls = [c for c in harness.calls if c.profile.name == "sister"]
    assert len(admin_calls) == 2 and len(sister_calls) == 1
    assert admin_calls[0].session.id == admin_calls[1].session.id
    assert sister_calls[0].session.id != admin_calls[0].session.id
    # The second admin turn resumes the harness session the first one produced.
    assert admin_calls[1].session.harness_session_id == "hs-admin-1"


async def test_voice_message_gets_a_text_reply_by_default(seeded: SqliteStore) -> None:
    harness, sink = FakeHarness(), FakeSink()
    await _orchestrator(seeded, harness, transcript="what time is it").handle(_voice("222"), sink)

    assert harness.calls[0].turn.text == "what time is it"
    assert [m.text for m in sink.sent] == ["echo: what time is it"]
    assert sink.audio == []
    assert "recording" not in sink.activities


async def test_agent_can_answer_with_voice_notes_and_skip_the_text(seeded: SqliteStore) -> None:
    sink = FakeSink()
    await _orchestrator(seeded, _SpeakingHarness()).handle(_text("222", "hi"), sink)

    assert sink.audio == [(b"OGG:hi", "hi")]
    assert sink.sent == []
    assert "recording" in sink.activities
    assert sink.activities[-1] == "idle"


async def test_voice_start_a_new_archives_and_confirms(seeded: SqliteStore) -> None:
    harness = FakeHarness()
    orchestrator = _orchestrator(seeded, harness, transcript="Start a new.")
    await orchestrator.handle(_text("222", "earlier question"), FakeSink())
    before = seeded.active_session("sister")

    sink = FakeSink()
    await orchestrator.handle(_voice("222"), sink)

    after = seeded.active_session("sister")
    assert before is not None and after is not None and after.id != before.id
    assert len(harness.calls) == 1  # the command never reached the agent
    assert sink.sent[-1].text == "Начали новый разговор."


async def test_typed_new_command(seeded: SqliteStore) -> None:
    harness = FakeHarness()
    await _orchestrator(seeded, harness).handle(_text("111", "/new"), FakeSink())
    assert harness.calls == []


async def test_harness_error_is_shown_and_session_is_kept(seeded: SqliteStore) -> None:
    harness = FakeHarness(fail_with="claude_failed")
    sink = FakeSink()
    await _orchestrator(seeded, harness).handle(_text("222", "hi"), sink)

    assert sink.sent[-1].text == "Claude не смог ответить. Попробуйте ещё раз."
    assert sink.activities[-1] == "idle"


async def test_profile_without_login_is_refused(seeded: SqliteStore) -> None:
    seeded.set_auth_token("sister", "")
    harness, sink = FakeHarness(), FakeSink()
    await _orchestrator(seeded, harness).handle(_text("222", "hi"), sink)

    assert harness.calls == []
    assert "не подключён к Claude" in sink.sent[-1].text


async def test_image_only_message_is_passed_as_images(seeded: SqliteStore) -> None:
    photo = Attachment("image", b"jpeg-bytes", "image/jpeg")
    harness = FakeHarness()
    message = InboundMessage(channel="telegram", sender_id="111", attachments=(photo,))
    await _orchestrator(seeded, harness).handle(message, FakeSink())

    turn = harness.calls[0].turn
    assert len(turn.images) == 1 and turn.images[0].mime_type == "image/jpeg"


async def test_ping_answers_pong_without_the_agent(seeded: SqliteStore) -> None:
    harness, sink = FakeHarness(), FakeSink()
    await _orchestrator(seeded, harness).handle(_text("222", "/ping"), sink)
    assert [m.text for m in sink.sent] == ["pong"]
    assert harness.calls == []


async def test_ping_works_for_a_family_member_without_a_profile(seeded: SqliteStore) -> None:
    seeded.sync_config(
        principals=[Principal(id="family", name="Family", role=Role.USER)],
        identities=[("telegram", "333", "family")],
        profiles=[],
    )
    sink = FakeSink()
    await _orchestrator(seeded, FakeHarness()).handle(_text("333", "/ping"), sink)
    assert [m.text for m in sink.sent] == ["pong"]


async def test_sister_gets_errors_in_russian(seeded: SqliteStore) -> None:
    harness = FakeHarness(fail_with="claude_login_invalid")
    sink = FakeSink()
    await _orchestrator(seeded, harness).handle(_text("222", "hi"), sink)
    assert "Вход в Claude" in sink.sent[-1].text


async def test_sister_status_and_new_session_are_russian(seeded: SqliteStore) -> None:
    orchestrator = _orchestrator(seeded, FakeHarness())
    sink = FakeSink()
    await orchestrator.handle(_text("222", "/new"), sink)
    assert sink.sent[-1].text == "Начали новый разговор."

    sink = FakeSink()
    await orchestrator.handle(_text("222", "/status"), sink)
    assert sink.sent[-1].text.startswith("Профиль: sister")


async def test_admin_errors_stay_english(seeded: SqliteStore) -> None:
    harness = FakeHarness(fail_with="claude_rate_limited")
    sink = FakeSink()
    await _orchestrator(seeded, harness).handle(_text("111", "hi"), sink)
    assert sink.sent[-1].text.startswith("Claude has hit its usage limit")


async def test_voice_quota_falls_back_to_the_text_reply(seeded: SqliteStore) -> None:

    class QuotaSpeaker:
        async def stream(self, text: str):
            yield SpeechPiece(text=text, error="voice_quota")

    orchestrator = Orchestrator(
        repo=seeded,
        harnesses=HarnessRegistry({"claude": _SpeakingHarness()}),
        transcriber=None,
        speaker=QuotaSpeaker(),
    )
    sink = FakeSink()
    await orchestrator.handle(_text("222", "hi"), sink)
    texts = [m.text for m in sink.sent]
    assert any("квота OpenAI" in t for t in texts)
    assert texts[-1] == "echo: hi"
    assert sink.audio == []


class _SpeakingHarness(FakeHarness):
    """Asks for voice notes for its text, then answers with the same text."""

    async def run_turn(
        self,
        *,
        profile,
        session,
        turn,
        editor,
        on_session_id=None,
        outbox: Outbox | None = None,
    ) -> TurnResult:
        self.calls.append(Call(profile=profile, session=session, turn=turn))
        assert outbox is not None
        await outbox.send_voice(turn.text)
        return TurnResult(text=f"echo: {turn.text}", harness_session_id="hs-voice")


class _BlockingHarness(FakeHarness):
    """Stays busy until cancelled, like a long agent turn."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run_turn(self, **kwargs):  # type: ignore[override]
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("never reached")


async def test_newer_message_pauses_the_running_turn(seeded: SqliteStore) -> None:
    harness = _BlockingHarness()
    orchestrator = _orchestrator(seeded, harness)
    first_sink, second_sink = FakeSink(), FakeSink()

    first = asyncio.create_task(orchestrator.handle(_text("222", "long question"), first_sink))
    await harness.started.wait()
    harness.started.clear()

    harness_second = FakeHarness()
    orchestrator._harnesses = HarnessRegistry({"claude": harness_second})
    await orchestrator.handle(_text("222", "actually, this one"), second_sink)
    await first

    assert first_sink.sent == []  # the paused turn says nothing
    assert [m.text for m in second_sink.sent] == ["echo: actually, this one"]


async def test_idle_session_is_renewed_with_a_notice(seeded: SqliteStore) -> None:
    harness = FakeHarness()
    orchestrator = _orchestrator(seeded, harness)
    await orchestrator.handle(_text("222", "first"), FakeSink())
    old = seeded.active_session("sister")
    assert old is not None

    seeded._conn.execute(
        "UPDATE sessions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (old.id,)
    )
    sink = FakeSink()
    await orchestrator.handle(_text("222", "back again"), sink)

    new = seeded.active_session("sister")
    assert new is not None and new.id != old.id
    assert sink.sent[0].text.startswith("Прошло много времени")
    assert sink.sent[-1].text == "echo: back again"


async def test_recent_session_is_kept(seeded: SqliteStore) -> None:
    orchestrator = _orchestrator(seeded, FakeHarness())
    await orchestrator.handle(_text("222", "one"), FakeSink())
    before = seeded.active_session("sister")
    await orchestrator.handle(_text("222", "two"), FakeSink())
    after = seeded.active_session("sister")
    assert before is not None and after is not None and after.id == before.id


async def test_document_is_passed_as_a_file(seeded: SqliteStore) -> None:
    doc = Attachment("file", b"%PDF-1.4", "application/pdf", "notes.pdf")
    harness = FakeHarness()
    message = InboundMessage(channel="telegram", sender_id="111", attachments=(doc,))
    await _orchestrator(seeded, harness).handle(message, FakeSink())

    files = harness.calls[0].turn.files
    assert len(files) == 1 and files[0].name == "notes.pdf"
    assert files[0].mime_type == "application/pdf"


async def test_file_without_caption_gets_a_placeholder(seeded: SqliteStore) -> None:
    csv = Attachment("file", b"a,b", "text/csv", "data.csv")
    harness = FakeHarness()
    message = InboundMessage(channel="telegram", sender_id="111", attachments=(csv,))
    await _orchestrator(seeded, harness).handle(message, FakeSink())
    assert (
        harness.calls[0].turn.text == "(отправлен файл)"
        or harness.calls[0].turn.text == "(sent a file)"
    )
