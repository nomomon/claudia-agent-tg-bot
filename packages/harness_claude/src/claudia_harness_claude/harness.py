from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    McpSdkServerConfig,
    ResultMessage,
    TextBlock,
    query,
)
from claudia_core import Profile, Session
from claudia_harness import HarnessError, Outbox, ProfileEditor, Turn, TurnResult

from claudia_harness_claude.control import CONTROL_SERVER, control_tool_names, make_control_server

log = logging.getLogger(__name__)

# Built-in Claude Code tools. Anything not listed in a profile's `tools` is disallowed.
BUILTIN_TOOLS = (
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
)


def build_env(
    profile: Profile,
    *,
    config_root: Path,
    scrub_env: Iterable[str],
) -> dict[str, str]:
    """Environment for the Claude Code subprocess.

    The SDK merges os.environ into the child, so bot secrets are blanked here.
    Each profile gets its own CLAUDE_CONFIG_DIR (login, memory, session transcripts).
    """
    env = {name: "" for name in scrub_env}
    env["CLAUDE_CONFIG_DIR"] = str(config_root / profile.name)
    if profile.auth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = profile.auth_token
    return env


def build_options(
    profile: Profile,
    session: Session,
    *,
    config_root: Path,
    scrub_env: Sequence[str],
    control: McpSdkServerConfig | None,
    control_tools: Sequence[str] | None = None,
) -> ClaudeAgentOptions:
    allowed = list(profile.tools)
    mcp_servers: dict[str, Any] = {}
    if control is not None:
        allowed.extend(control_tools if control_tools is not None else control_tool_names())
        mcp_servers[CONTROL_SERVER] = control

    return ClaudeAgentOptions(
        model=profile.model,
        cwd=profile.working_dir,
        env=build_env(profile, config_root=config_root, scrub_env=scrub_env),
        system_prompt=profile.system_prompt,
        allowed_tools=allowed,
        disallowed_tools=[t for t in BUILTIN_TOOLS if t not in profile.tools],
        permission_mode="dontAsk",
        resume=session.harness_session_id,
        mcp_servers=mcp_servers,
    )


# Files Claude can read natively, as content blocks. Everything else goes to disk.
NATIVE_DOCUMENT_TYPES = ("application/pdf",)


def _inbox_name(name: str) -> str:
    """A filesystem-safe name. Keeps the extension, drops anything path-like."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return safe.lstrip(".") or "file"


def _kib(size: int) -> str:
    return f"{max(1, round(size / 1024))} KB"


def _describe_turn_files(
    profile: Profile,
    turn: Turn,
    inbox: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split a turn's files into content blocks and notes for the prompt text.

    PDFs go in as document blocks. Other files are saved under the bot's data folder and the
    path is given, but only when the profile can read files. Profiles without a reading tool
    are told the file exists, so the reply can say so honestly.
    """
    blocks: list[dict[str, Any]] = []
    notes: list[str] = []
    can_read = "Read" in profile.tools
    for file in turn.files:
        if not file.data:
            notes.append(f"[The person sent a file called {file.name}, too large for the bot.]")
        elif file.mime_type in NATIVE_DOCUMENT_TYPES:
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": file.mime_type,
                        "data": base64.b64encode(file.data).decode("ascii"),
                    },
                }
            )
        elif can_read:
            inbox.mkdir(parents=True, exist_ok=True)
            path = inbox / f"{uuid4().hex[:8]}-{_inbox_name(file.name)}"
            path.write_bytes(file.data)
            notes.append(
                f"[The person sent a file ({file.mime_type}, {_kib(len(file.data))}), "
                f"saved at {path}. Read it if it helps.]"
            )
        else:
            notes.append(
                f"[The person sent a file called {file.name} ({file.mime_type}, "
                f"{_kib(len(file.data))}). You cannot open it in this chat.]"
            )
    return blocks, notes


async def _single_prompt(content: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def _content_blocks(
    turn: Turn, extra: list[dict[str, Any]], notes: list[str]
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for image in turn.images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.mime_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                },
            }
        )
    content.extend(extra)
    text = "\n".join([turn.text, *notes]).strip()
    content.append({"type": "text", "text": text or "(no text)"})
    return content


def _error_key(status: int | None) -> str:
    """Map an Anthropic API status to a message key. Anything unknown is a generic failure."""
    if status == 401:
        return "claude_login_invalid"
    if status == 429:
        return "claude_rate_limited"
    if status is not None and status >= 500:
        return "claude_overloaded"
    return "claude_failed"


class ClaudeCodeHarness:
    kind = "claude"

    def __init__(self, *, config_root: Path, scrub_env: Sequence[str] = ()) -> None:
        self._config_root = config_root
        self._scrub_env = tuple(scrub_env)

    async def run_turn(
        self,
        *,
        profile: Profile,
        session: Session,
        turn: Turn,
        editor: ProfileEditor,
        on_session_id: Callable[[str], None] | None = None,
        outbox: Outbox | None = None,
    ) -> TurnResult:
        if not profile.auth_token:
            raise HarnessError("not_connected")

        inbox = self._config_root / "inbox" / profile.name
        can_send = profile.allow_outbox and outbox is not None
        control = None
        if profile.allow_self_control or can_send:
            # The agent may send files from its working folder and from the bot's inbox.
            control = make_control_server(
                profile,
                editor,
                outbox=outbox,
                sendable_roots=[Path(profile.working_dir), inbox],
            )
        options = build_options(
            profile,
            session,
            config_root=self._config_root,
            scrub_env=self._scrub_env,
            control=control,
            control_tools=control_tool_names(
                self_control=profile.allow_self_control, outbox=can_send
            ),
        )
        extra, notes = _describe_turn_files(profile, turn, inbox)
        if turn.images or extra:
            prompt: Any = _single_prompt(_content_blocks(turn, extra, notes))
        else:
            prompt = turn.text

        parts: list[str] = []
        result_text: str | None = None
        harness_session_id: str | None = None
        try:
            # query() is an async generator; aclosing stops the CLI subprocess when a turn is
            # cancelled (for example, when the person sends a newer message).
            stream = cast(AsyncGenerator[Any, None], query(prompt=prompt, options=options))
            async with aclosing(stream) as messages:
                async for message in messages:
                    seen = getattr(message, "session_id", None)
                    if seen and seen != harness_session_id:
                        harness_session_id = seen
                        if on_session_id is not None:
                            on_session_id(seen)
                    if isinstance(message, AssistantMessage):
                        parts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                    elif isinstance(message, ResultMessage):
                        if message.is_error:
                            log.error(
                                "claude turn failed: subtype=%s status=%s errors=%s",
                                message.subtype,
                                message.api_error_status,
                                message.errors,
                            )
                            raise HarnessError(_error_key(message.api_error_status))
                        result_text = message.result
        except HarnessError:
            raise
        except asyncio.CancelledError:
            # A newer message replaced this turn. Let the cancellation propagate.
            raise
        except Exception as exc:
            log.exception("claude harness crashed for profile %s", profile.name)
            raise HarnessError("claude_crashed") from exc

        text = (result_text if result_text is not None else "".join(parts)).strip()
        return TurnResult(text=text or "(no reply)", harness_session_id=harness_session_id)
