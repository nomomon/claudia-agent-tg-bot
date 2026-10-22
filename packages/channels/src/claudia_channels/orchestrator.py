from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from claudia_core import Attachment, InboundMessage, OutboundMessage, Profile
from claudia_harness import HarnessError, HarnessRegistry, Turn, TurnImage

from claudia_channels.commands import Command, parse_command
from claudia_channels.port import ReplySink, Repository, Speaker, Transcriber

log = logging.getLogger(__name__)

NO_PROFILE = "You don't have a profile set up yet."
VOICE_OFF = "Voice messages are not enabled on this bot."


class Orchestrator:
    """Turns one inbound message into one reply, for any channel and any harness.

    Isolation: every lookup goes through the sender's principal, so one person's
    profile and sessions are never reachable from another person's message.
    """

    def __init__(
        self,
        *,
        repo: Repository,
        harnesses: HarnessRegistry,
        transcriber: Transcriber | None = None,
        speaker: Speaker | None = None,
    ) -> None:
        self._repo = repo
        self._harnesses = harnesses
        self._transcriber = transcriber
        self._speaker = speaker
        # One turn at a time per principal, so two fast messages cannot fork a session.
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def handle(self, msg: InboundMessage, sink: ReplySink) -> None:
        principal = self._repo.principal_for_identity(msg.channel, msg.sender_id)
        if principal is None:
            # Unknown senders get no reply at all, so the bot does not confirm it exists.
            log.info("ignored message from unknown %s sender", msg.channel)
            return

        # Liveness check. Answered before any profile or agent work, so it tells you the
        # bot is up even when a profile or harness is broken.
        if msg.text and parse_command(msg.text, from_voice=False) is Command.PING:
            await self._send_text(sink, "pong")
            return

        async with self._locks[principal.id]:
            profile = self._repo.profile_for_principal(principal.id)
            if profile is None:
                await self._send_text(sink, NO_PROFILE)
                return
            try:
                await self._dispatch(msg, profile, sink)
            except HarnessError as exc:
                await self._send_text(sink, str(exc))

    async def _dispatch(self, msg: InboundMessage, profile: Profile, sink: ReplySink) -> None:
        text, from_voice = await self._text_of(msg, sink, profile)
        if text is None:
            return

        command = parse_command(text, from_voice=from_voice)
        if command is Command.NEW_SESSION:
            self._repo.start_new_session(profile.name)
            await self._reply(sink, profile, "Started a new session.", voice=from_voice)
            return
        if command is Command.STATUS:
            await self._reply(sink, profile, _describe(profile), voice=from_voice)
            return

        if not profile.auth_token:
            await self._send_text(sink, "This profile is not connected to Claude yet.")
            return

        harness = self._harnesses.get(profile.harness)
        session = self._repo.ensure_session(profile.name)
        turn = Turn(text=text, images=tuple(_images(msg.attachments)))

        await sink.activity("typing")
        try:
            result = await harness.run_turn(
                profile=profile,
                session=session,
                turn=turn,
                editor=self._repo,
            )
        finally:
            await sink.activity("idle")

        if result.harness_session_id:
            self._repo.set_harness_session_id(session.id, result.harness_session_id)
        await self._reply(sink, profile, result.text, voice=from_voice or profile.voice_reply)

    async def _text_of(
        self, msg: InboundMessage, sink: ReplySink, profile: Profile
    ) -> tuple[str | None, bool]:
        """Return the text to act on, and whether it came from speech."""
        parts: list[str] = []
        from_voice = False
        for attachment in msg.attachments:
            if attachment.kind != "audio":
                continue
            from_voice = True
            if self._transcriber is None:
                await self._send_text(sink, VOICE_OFF)
                return None, True
            parts.append(await self._transcriber.transcribe(attachment.data, attachment.mime_type))
        if msg.text:
            parts.insert(0, msg.text)

        text = " ".join(p for p in parts if p).strip()
        if not text and not _images(msg.attachments):
            return None, from_voice
        return text or "(sent an image)", from_voice

    async def _reply(self, sink: ReplySink, profile: Profile, text: str, *, voice: bool) -> None:
        if not voice or self._speaker is None:
            await self._send_text(sink, text)
            return
        await sink.activity("recording")
        try:
            audio = await self._speaker.synthesize(text)
        finally:
            await sink.activity("idle")
        await sink.send(OutboundMessage(text=text, audio=audio))

    async def _send_text(self, sink: ReplySink, text: str) -> None:
        await sink.send(OutboundMessage(text=text))


def _images(attachments: tuple[Attachment, ...]) -> list[TurnImage]:
    return [TurnImage(data=a.data, mime_type=a.mime_type) for a in attachments if a.kind == "image"]


def _describe(profile: Profile) -> str:
    return (
        f"Profile: {profile.name}\n"
        f"Model: {profile.model or 'default'}\n"
        f"Voice replies: {'on' if profile.voice_reply else 'off'}"
    )
