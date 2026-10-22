from claudia_channels import Orchestrator
from claudia_core import Attachment, InboundMessage, Principal, Role
from claudia_harness import HarnessRegistry
from claudia_store import SqliteStore

from tests.fakes import FakeHarness, FakeSink, FakeSpeaker, FakeTranscriber


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


async def test_sister_voice_message_gets_voice_reply(seeded: SqliteStore) -> None:
    harness, sink = FakeHarness(), FakeSink()
    await _orchestrator(seeded, harness, transcript="what time is it").handle(_voice("222"), sink)

    assert harness.calls[0].turn.text == "what time is it"
    assert any(m.audio == b"OGG:echo: what time is it" for m in sink.sent)
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
    assert sink.sent[-1].text == "Started a new session."


async def test_typed_new_command(seeded: SqliteStore) -> None:
    harness = FakeHarness()
    await _orchestrator(seeded, harness).handle(_text("111", "/new"), FakeSink())
    assert harness.calls == []


async def test_harness_error_is_shown_and_session_is_kept(seeded: SqliteStore) -> None:
    harness = FakeHarness(fail_with="Claude could not answer. Please try again.")
    sink = FakeSink()
    await _orchestrator(seeded, harness).handle(_text("222", "hi"), sink)

    assert sink.sent[-1].text == "Claude could not answer. Please try again."
    assert sink.activities[-1] == "idle"


async def test_profile_without_login_is_refused(seeded: SqliteStore) -> None:
    seeded.set_auth_token("sister", "")
    harness, sink = FakeHarness(), FakeSink()
    await _orchestrator(seeded, harness).handle(_text("222", "hi"), sink)

    assert harness.calls == []
    assert "not connected" in sink.sent[-1].text


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
