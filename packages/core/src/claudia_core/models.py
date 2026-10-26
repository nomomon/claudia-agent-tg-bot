from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True)
class Principal:
    """A person allowed to use the bot. Everything else is scoped to a principal."""

    id: str
    name: str
    role: Role


@dataclass(frozen=True)
class Profile:
    """One harness setup: whose subscription, which folder, which tools.

    Profile names are globally unique. A principal owns one or more profiles.
    """

    name: str
    principal_id: str
    harness: str
    working_dir: str
    tools: tuple[str, ...] = ()
    model: str | None = None
    models: tuple[str, ...] = ()
    allow_self_control: bool = False
    voice_reply: bool = False
    system_prompt: str | None = None
    language: str = "en"
    allow_outbox: bool = False
    auth_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Session:
    """A conversation with a harness. Resumed by harness_session_id until archived."""

    id: int
    profile_name: str
    harness_session_id: str | None
    status: Literal["active", "archived"]


AttachmentKind = Literal["audio", "image", "video", "file"]


@dataclass(frozen=True)
class Attachment:
    """Any media a person sent. `kind` decides how the bot handles it.

    `filename` is the name Telegram reports, when there is one.
    """

    kind: AttachmentKind
    data: bytes = field(repr=False)
    mime_type: str
    filename: str | None = None


@dataclass(frozen=True)
class OutgoingFile:
    """A file the agent asked to send. `kind` is "photo" (shown inline) or "document"."""

    kind: Literal["photo", "document"]
    name: str
    data: bytes = field(repr=False)
    mime_type: str
    caption: str = ""


@dataclass(frozen=True)
class InboundMessage:
    """A channel-neutral message. Channels build these, the orchestrator consumes them."""

    channel: str
    sender_id: str
    text: str | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class OutboundMessage:
    text: str


@dataclass(frozen=True)
class SpeechPiece:
    """One segment of a spoken reply: either its Ogg Opus audio, or the message key for why not."""

    text: str = ""
    audio: bytes | None = field(default=None, repr=False)
    error: str | None = None
