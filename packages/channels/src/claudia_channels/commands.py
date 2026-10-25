"""Commands that never reach the agent.

Typed commands are exact ("/new"). Voice commands are matched as whole short utterances,
so a real question that happens to mention "new" still goes to the agent.
"""

from __future__ import annotations

import re
from enum import Enum

MAX_VOICE_COMMAND_WORDS = 6

_NEW_SESSION_VOICE = (
    r"(start|begin|open|make|create)( a| an)? (new|fresh)( session| chat| conversation)?",
    r"new (session|chat|conversation)",
    r"(start|begin) over",
    r"reset( the)?( session| chat| conversation)?",
)
_VOICE_PATTERN = re.compile(
    r"(please )?(" + "|".join(f"(?:{p})" for p in _NEW_SESSION_VOICE) + r")( please)?",
)


class Command(Enum):
    NEW_SESSION = "new_session"
    STATUS = "status"
    PING = "ping"


_TYPED = {"/new": Command.NEW_SESSION, "/status": Command.STATUS, "/ping": Command.PING}


def parse_command(text: str, *, from_voice: bool) -> Command | None:
    cleaned = text.strip()
    if not from_voice:
        return _TYPED.get(cleaned.lower())

    normalized = re.sub(r"[^\w\s]", "", cleaned.lower()).strip()
    if not normalized or len(normalized.split()) > MAX_VOICE_COMMAND_WORDS:
        return None
    if _VOICE_PATTERN.fullmatch(normalized):
        return Command.NEW_SESSION
    return None
