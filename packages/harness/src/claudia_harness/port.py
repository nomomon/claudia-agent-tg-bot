from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from claudia_core import OutgoingFile, Profile, ScheduledJob, Session, UserFacingError


class HarnessError(UserFacingError):
    """A turn could not complete. `key` picks the message shown to the user."""


@dataclass(frozen=True)
class TurnImage:
    data: bytes = field(repr=False)
    mime_type: str


@dataclass(frozen=True)
class TurnFile:
    name: str
    data: bytes = field(repr=False)
    mime_type: str


@dataclass(frozen=True)
class Turn:
    text: str
    images: tuple[TurnImage, ...] = ()
    files: tuple[TurnFile, ...] = ()


@dataclass(frozen=True)
class TurnResult:
    text: str
    harness_session_id: str | None


class ProfileEditor(Protocol):
    """What a harness may change about its own profile (self-control).

    The orchestrator passes the store here. Harnesses only see this narrow surface.
    """

    async def set_model(self, profile_name: str, model: str) -> None: ...

    async def get_profile(self, profile_name: str) -> Profile: ...


class Outbox(Protocol):
    """How a running turn sends things to the person, before or instead of its final reply."""

    async def send_text(self, text: str) -> None: ...

    async def send_file(self, file: OutgoingFile) -> None: ...

    async def send_voice(self, text: str) -> bool:
        """Speak the text as voice notes. False if none could be made."""
        ...


class Messenger(Protocol):
    """How a running turn hands a message to another profile's own agent, if it is allowed to."""

    async def send_to(self, to_profile: str, text: str) -> str:
        """Deliver text as a turn for to_profile. Returns a short outcome for the agent to read."""
        ...


class Scheduler(Protocol):
    """How a running turn schedules its own profile to be woken up later.

    Bound to the calling profile already, so the agent never names a profile here.
    """

    async def schedule(
        self, *, prompt: str, title: str, at: str | None = None, cron: str | None = None
    ) -> ScheduledJob:
        """Create a one-off (`at`) or repeating (`cron`) job. Raises SchedulerError."""
        ...

    async def list_tasks(self) -> list[ScheduledJob]: ...

    async def delete(self, job_id: str) -> bool: ...

    def now(self) -> datetime:
        """Current time in the scheduler's configured timezone."""
        ...


class Harness(Protocol):
    kind: str

    async def run_turn(
        self,
        *,
        profile: Profile,
        session: Session,
        turn: Turn,
        editor: ProfileEditor,
        on_session_id: Callable[[str], None] | None = None,
        outbox: Outbox | None = None,
        messenger: Messenger | None = None,
        scheduler: Scheduler | None = None,
    ) -> TurnResult:
        """Run one user turn. Raise HarnessError for failures the user should see.

        on_session_id is called as soon as the harness knows its session id, so a turn that is
        cancelled midway still leaves the session resumable.
        """
        ...


class HarnessRegistry:
    def __init__(self, harnesses: dict[str, Harness] | None = None) -> None:
        self._harnesses: dict[str, Harness] = dict(harnesses or {})

    def register(self, harness: Harness) -> None:
        self._harnesses[harness.kind] = harness

    def get(self, kind: str) -> Harness:
        try:
            return self._harnesses[kind]
        except KeyError:
            raise HarnessError("no_harness") from None
