from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from claudia_core import OutboundMessage, OutgoingFile, Profile, Session
from claudia_harness import HarnessError, ProfileEditor, Turn, TurnResult


@dataclass
class FakeSink:
    activities: list[str] = field(default_factory=list)
    sent: list[OutboundMessage] = field(default_factory=list)
    files: list[OutgoingFile] = field(default_factory=list)

    async def activity(self, kind: str) -> None:
        self.activities.append(kind)

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    async def send_interim(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    async def send_file(self, file: OutgoingFile) -> None:
        self.files.append(file)


@dataclass
class Call:
    profile: Profile
    session: Session
    turn: Turn


@dataclass
class FakeHarness:
    """Records every turn and answers with a predictable reply."""

    kind: str = "claude"
    calls: list[Call] = field(default_factory=list)
    fail_with: str | None = None
    _counter: int = 0

    async def run_turn(
        self,
        *,
        profile: Profile,
        session: Session,
        turn: Turn,
        editor: ProfileEditor,
        on_session_id: Callable[[str], None] | None = None,
        outbox: object | None = None,
    ) -> TurnResult:
        self.calls.append(Call(profile=profile, session=session, turn=turn))
        if self.fail_with:
            raise HarnessError(self.fail_with)
        self._counter += 1
        harness_session_id = session.harness_session_id or f"hs-{profile.name}-{self._counter}"
        if on_session_id is not None:
            on_session_id(harness_session_id)
        return TurnResult(text=f"echo: {turn.text}", harness_session_id=harness_session_id)


class FakeSpeaker:
    async def synthesize(self, text: str) -> bytes:
        return f"OGG:{text}".encode()


class FakeTranscriber:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        return self.transcript
