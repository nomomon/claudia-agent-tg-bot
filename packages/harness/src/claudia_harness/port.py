from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from claudia_core import Profile, Session, UserFacingError


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
