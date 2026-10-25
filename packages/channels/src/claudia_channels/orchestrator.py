from __future__ import annotations

import asyncio
import logging
import mimetypes
from dataclasses import dataclass, field
from datetime import timedelta

from claudia_core import (
    Attachment,
    InboundMessage,
    OutboundMessage,
    OutgoingFile,
    Profile,
    UserFacingError,
)
from claudia_harness import HarnessRegistry, Turn, TurnFile, TurnImage

from claudia_channels import messages
from claudia_channels.commands import Command, parse_command
from claudia_channels.port import ReplySink, Repository, Speaker, Transcriber

log = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"
DEFAULT_SESSION_IDLE = timedelta(hours=6)


@dataclass
class _Inbound:
    """One inbound message, read into the parts a turn needs."""

    text: str | None = None
    from_voice: bool = False
    images: list[TurnImage] = field(default_factory=list)
    files: list[TurnFile] = field(default_factory=list)


class Orchestrator:
    """Turns inbound messages into replies, for any channel and any harness.

    Isolation: every lookup goes through the sender's principal, so one person's
    profile and sessions are never reachable from another person's message.

    Interruption: a newer message from the same person cancels the turn that is running and
    starts at once. Nothing waits in a queue. The cancelled turn sends nothing.
    """

    def __init__(
        self,
        *,
        repo: Repository,
        harnesses: HarnessRegistry,
        transcriber: Transcriber | None = None,
        speaker: Speaker | None = None,
        session_idle: timedelta = DEFAULT_SESSION_IDLE,
    ) -> None:
        self._repo = repo
        self._harnesses = harnesses
        self._transcriber = transcriber
        self._speaker = speaker
        self._session_idle = session_idle
        self._turns: dict[str, asyncio.Task[None]] = {}

    async def handle(self, msg: InboundMessage, sink: ReplySink) -> None:
        principal = self._repo.principal_for_identity(msg.channel, msg.sender_id)
        if principal is None:
            # Unknown senders get no reply at all, so the bot does not confirm it exists.
            log.info("ignored message from unknown %s sender", msg.channel)
            return

        # Liveness check. Answered before any profile or agent work, so it tells you the
        # bot is up even when a profile or harness is broken.
        if msg.text and parse_command(msg.text, from_voice=False) is Command.PING:
            await sink.send(OutboundMessage(text="pong"))
            return

        profile = self._repo.profile_for_principal(principal.id)
        if profile is None:
            await self._say(sink, DEFAULT_LANGUAGE, "no_profile")
            return

        await self._replace_running_turn(profile.name)
        task = asyncio.create_task(self._run(msg, profile, sink))
        self._turns[profile.name] = task
        await asyncio.wait({task})
        if self._turns.get(profile.name) is task:
            del self._turns[profile.name]
        if not task.cancelled() and task.exception() is not None:
            log.error("turn failed unexpectedly", exc_info=task.exception())

    async def _replace_running_turn(self, profile_name: str) -> None:
        running = self._turns.get(profile_name)
        if running is None or running.done():
            return
        log.info("newer message for %s, pausing the running turn", profile_name)
        running.cancel()
        await asyncio.wait({running})

    async def _run(self, msg: InboundMessage, profile: Profile, sink: ReplySink) -> None:
        try:
            await self._dispatch(msg, profile, sink)
        except UserFacingError as exc:
            await self._say(sink, profile.language, exc.key)

    async def _dispatch(self, msg: InboundMessage, profile: Profile, sink: ReplySink) -> None:
        inbound = await self._read(msg, profile)
        if inbound is None:
            return

        text = inbound.text or ""
        command = parse_command(text, from_voice=inbound.from_voice)
        if command is Command.NEW_SESSION:
            self._repo.start_new_session(profile.name)
            await self._reply(sink, profile, _t(profile, "new_session"), voice=inbound.from_voice)
            return
        if command is Command.STATUS:
            await self._reply(sink, profile, _describe(profile), voice=inbound.from_voice)
            return

        if not profile.auth_token:
            raise UserFacingError("not_connected")

        if self._repo.is_idle(profile.name, self._session_idle):
            self._repo.start_new_session(profile.name)
            await self._say(sink, profile.language, "session_renewed")

        harness = self._harnesses.get(profile.harness)
        session = self._repo.ensure_session(profile.name)
        self._repo.touch_session(session.id)
        turn = Turn(text=text, images=tuple(inbound.images), files=tuple(inbound.files))

        def remember(harness_session_id: str) -> None:
            # Saved as soon as it is known, so an interrupted turn can still be resumed.
            self._repo.set_harness_session_id(session.id, harness_session_id)

        await sink.activity("typing")
        try:
            result = await harness.run_turn(
                profile=profile,
                session=session,
                turn=turn,
                editor=self._repo,
                on_session_id=remember,
                outbox=_TurnOutbox(sink),
            )
        finally:
            await sink.activity("idle")

        if result.harness_session_id:
            remember(result.harness_session_id)
        reply = result.text or _t(profile, "no_reply")
        await self._reply(sink, profile, reply, voice=inbound.from_voice or profile.voice_reply)

    async def _read(self, msg: InboundMessage, profile: Profile) -> _Inbound | None:
        """Turn the message's attachments and text into what the turn needs.

        Returns None when there is nothing to act on.
        """
        inbound = _Inbound()
        spoken: list[str] = []
        for number, attachment in enumerate(msg.attachments, start=1):
            if attachment.kind == "audio":
                inbound.from_voice = True
                spoken.append(await self._transcribe(attachment))
            elif attachment.kind == "image":
                if attachment.data:
                    inbound.images.append(TurnImage(attachment.data, attachment.mime_type))
            else:
                inbound.files.append(_as_turn_file(attachment, number))

        parts = [msg.text or "", *spoken]
        text = " ".join(p for p in parts if p).strip()
        if not text and not inbound.images and not inbound.files:
            return None
        if not text:
            text = _t(profile, "image_only" if inbound.images else "file_only")
        inbound.text = text
        return inbound

    async def _transcribe(self, attachment: Attachment) -> str:
        if self._transcriber is None:
            raise UserFacingError("voice_off")
        try:
            return await self._transcriber.transcribe(attachment.data, attachment.mime_type)
        except UserFacingError:
            raise
        except Exception:
            log.exception("transcription failed")
            raise UserFacingError("voice_failed") from None

    async def _reply(self, sink: ReplySink, profile: Profile, text: str, *, voice: bool) -> None:
        if not voice or self._speaker is None:
            await sink.send(OutboundMessage(text=text))
            return
        await sink.activity("recording")
        try:
            audio = await self._speaker.synthesize(text)
        except UserFacingError as exc:
            # The text still goes out. Only the voice part is lost, and the reason is said.
            await sink.send(OutboundMessage(text=text))
            await self._say(sink, profile.language, exc.key)
            return
        finally:
            await sink.activity("idle")
        await sink.send(OutboundMessage(text=text, audio=audio))

    async def _say(self, sink: ReplySink, language: str, key: str) -> None:
        await sink.send(OutboundMessage(text=messages.text(key, language)))


class _TurnOutbox:
    """What a running turn may send before it finishes. Wraps the channel's reply sink."""

    def __init__(self, sink: ReplySink) -> None:
        self._sink = sink

    async def send_text(self, text: str) -> None:
        await self._sink.send_interim(OutboundMessage(text=text))

    async def send_file(self, file: OutgoingFile) -> None:
        await self._sink.send_file(file)


def _as_turn_file(attachment: Attachment, number: int) -> TurnFile:
    name = attachment.filename
    if not name:
        extension = mimetypes.guess_extension(attachment.mime_type) or ""
        name = f"{attachment.kind}-{number}{extension}"
    return TurnFile(name=name, data=attachment.data, mime_type=attachment.mime_type)


def _t(profile: Profile, key: str) -> str:
    return messages.text(key, profile.language)


def _describe(profile: Profile) -> str:
    return messages.text(
        "status",
        profile.language,
        name=profile.name,
        model=profile.model or _t(profile, "default_model"),
        voice=_t(profile, "on" if profile.voice_reply else "off"),
    )
