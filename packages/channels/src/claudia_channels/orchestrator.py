from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
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
from claudia_channels.port import ReplySink, Repository, SinkFactory, Speaker, Transcriber

# Profiles currently in a relay chain for the running asyncio task (and its children).
# Guards message_user against loops without cancelling a turn's own ancestor.
_relay_chain: ContextVar[frozenset[str]] = ContextVar("relay_chain", default=frozenset())

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
        sink_factories: dict[str, SinkFactory] | None = None,
    ) -> None:
        self._repo = repo
        self._harnesses = harnesses
        self._transcriber = transcriber
        self._speaker = speaker
        self._session_idle = session_idle
        self._sink_factories: dict[str, SinkFactory] = dict(sink_factories or {})
        self._turns: dict[str, asyncio.Task[None]] = {}

    def register_sink_factory(self, channel: str, factory: SinkFactory) -> None:
        """Let the orchestrator reach a profile's own chat on `channel` without an inbound
        message, so one profile's message_user call can deliver into another profile's chat."""
        self._sink_factories[channel] = factory

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
        await self._run_text_turn(
            inbound.text or "",
            profile,
            sink,
            images=tuple(inbound.images),
            files=tuple(inbound.files),
            from_voice=inbound.from_voice,
        )

    async def _run_text_turn(
        self,
        text: str,
        profile: Profile,
        sink: ReplySink,
        *,
        images: tuple[TurnImage, ...] = (),
        files: tuple[TurnFile, ...] = (),
        from_voice: bool = False,
    ) -> None:
        command = parse_command(text, from_voice=from_voice)
        if command is Command.NEW_SESSION:
            self._repo.start_new_session(profile.name)
            await sink.send(OutboundMessage(text=_t(profile, "new_session")))
            return
        if command is Command.STATUS:
            await sink.send(OutboundMessage(text=_describe(profile)))
            return

        if not profile.auth_token:
            raise UserFacingError("not_connected")

        if self._repo.is_idle(profile.name, self._session_idle):
            self._repo.start_new_session(profile.name)
            await self._say(sink, profile.language, "session_renewed")

        harness = self._harnesses.get(profile.harness)
        session = self._repo.ensure_session(profile.name)
        self._repo.touch_session(session.id)
        turn = Turn(text=text, images=images, files=files)

        def remember(harness_session_id: str) -> None:
            # Saved as soon as it is known, so an interrupted turn can still be resumed.
            self._repo.set_harness_session_id(session.id, harness_session_id)

        outbox = _TurnOutbox(sink, lambda spoken: self._speak(sink, profile, spoken))
        messenger = _AgentMessenger(self, profile.name) if profile.message_targets else None
        await sink.activity("typing")
        started = time.perf_counter()
        try:
            result = await harness.run_turn(
                profile=profile,
                session=session,
                turn=turn,
                editor=self._repo,
                on_session_id=remember,
                outbox=outbox,
                messenger=messenger,
            )
        finally:
            await sink.activity("idle")

        if result.harness_session_id:
            remember(result.harness_session_id)
        log.info(
            "timing turn=%.2fs profile=%s chars=%d",
            time.perf_counter() - started,
            profile.name,
            len(result.text),
        )
        if outbox.spoke:
            # The voice notes already carried the answer, and each one has its text as caption.
            return
        await sink.send(OutboundMessage(text=result.text or _t(profile, "no_reply")))

    async def _deliver_from_agent(self, from_profile: str, to_profile: str, text: str) -> str:
        """Run text as a turn for to_profile, delivered through its own channel identity.

        Called from inside from_profile's running turn (the message_user tool). A profile
        already in the current relay chain is refused outright, rather than cancelled, since
        cancelling it here would mean cancelling an in-flight ancestor of this very call.
        """
        chain = _relay_chain.get()
        if to_profile == from_profile or to_profile in chain:
            return "Not delivered: that would create a message loop."
        try:
            target = self._repo.load_profile(to_profile)
        except KeyError:
            return f"Not delivered: no profile named '{to_profile}'."
        identity = self._repo.identity_for_profile(to_profile)
        if identity is None:
            return f"Not delivered: '{to_profile}' has no channel to reach."
        channel, external_id = identity
        factory = self._sink_factories.get(channel)
        if factory is None:
            return f"Not delivered: no '{channel}' channel is wired up."
        sink = factory.sink_for(external_id)
        sender = self._repo.load_profile(from_profile)

        token = _relay_chain.set(chain | {from_profile})
        try:
            await self._replace_running_turn(target.name)
            task = asyncio.create_task(self._run_relayed(target, sink, sender.name, text))
            self._turns[target.name] = task
            await asyncio.wait({task})
            if self._turns.get(target.name) is task:
                del self._turns[target.name]
        finally:
            _relay_chain.reset(token)

        if task.cancelled():
            return "Not delivered: interrupted by a newer message to them."
        if task.exception() is not None:
            log.error("relayed turn to %s failed", target.name, exc_info=task.exception())
            return "Not delivered: their agent failed to answer."
        return "Delivered."

    async def _run_relayed(
        self, profile: Profile, sink: ReplySink, from_name: str, text: str
    ) -> None:
        try:
            await self._run_text_turn(f"[Message from {from_name}] {text}", profile, sink)
        except UserFacingError as exc:
            await self._say(sink, profile.language, exc.key)

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

    async def _speak(self, sink: ReplySink, profile: Profile, text: str) -> bool:
        """Send the text as voice notes, each as it is made. False if none could be made."""
        if self._speaker is None:
            return False
        await sink.activity("recording")
        started = time.perf_counter()
        spoke = False
        try:
            # aclosing stops speech still in progress when this turn is cancelled.
            async with aclosing(self._speaker.stream(text)) as pieces:
                index = 0
                async for piece in pieces:
                    if piece.audio is not None:
                        index += 1
                        log.info(
                            "timing voice_note=%d at=%.2fs", index, time.perf_counter() - started
                        )
                        await sink.send_audio(piece.audio, piece.text)
                        spoke = True
                    elif piece.error is not None:
                        await self._say(sink, profile.language, piece.error)
        finally:
            await sink.activity("idle")
        return spoke

    async def _say(self, sink: ReplySink, language: str, key: str) -> None:
        await sink.send(OutboundMessage(text=messages.text(key, language)))


class _AgentMessenger:
    """Lets a running turn hand text to another profile's own agent, via message_user."""

    def __init__(self, orchestrator: Orchestrator, from_profile: str) -> None:
        self._orchestrator = orchestrator
        self._from_profile = from_profile

    async def send_to(self, to_profile: str, text: str) -> str:
        return await self._orchestrator._deliver_from_agent(self._from_profile, to_profile, text)


class _TurnOutbox:
    """What a running turn may send before it finishes. Speaks through the channel's sink."""

    def __init__(self, sink: ReplySink, speak: Callable[[str], Awaitable[bool]]) -> None:
        self._sink = sink
        self._speak = speak
        self.spoke = False

    async def send_text(self, text: str) -> None:
        await self._sink.send_interim(OutboundMessage(text=text))

    async def send_file(self, file: OutgoingFile) -> None:
        await self._sink.send_file(file)

    async def send_voice(self, text: str) -> bool:
        sent = await self._speak(text)
        self.spoke = self.spoke or sent
        return sent


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
