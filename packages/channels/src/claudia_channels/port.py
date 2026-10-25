from __future__ import annotations

from datetime import timedelta
from typing import Literal, Protocol

from claudia_core import OutboundMessage, OutgoingFile, Principal, Profile, Session
from claudia_harness import ProfileEditor

Activity = Literal["typing", "recording", "idle"]


class ReplySink(Protocol):
    """Where replies for one inbound message go. Implemented per channel."""

    async def activity(self, kind: Activity) -> None:
        """Show typing or recording. 'idle' stops it."""
        ...

    async def send(self, message: OutboundMessage) -> None: ...

    async def send_interim(self, message: OutboundMessage) -> None:
        """Send while the turn is still running. Keeps the typing indicator going."""
        ...

    async def send_file(self, file: OutgoingFile) -> None: ...


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, mime_type: str) -> str: ...


class Speaker(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class Repository(ProfileEditor, Protocol):
    """Persistence the orchestrator needs. Satisfied by claudia_store.SqliteStore."""

    def principal_for_identity(self, channel: str, external_id: str) -> Principal | None: ...

    def profile_for_principal(self, principal_id: str) -> Profile | None: ...

    def load_profile(self, profile_name: str) -> Profile: ...

    def ensure_session(self, profile_name: str) -> Session: ...

    def start_new_session(self, profile_name: str) -> Session: ...

    def set_harness_session_id(self, session_id: int, harness_session_id: str) -> None: ...

    def touch_session(self, session_id: int) -> None: ...

    def is_idle(self, profile_name: str, idle_after: timedelta) -> bool: ...
