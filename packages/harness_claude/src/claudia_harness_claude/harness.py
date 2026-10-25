from __future__ import annotations

import base64
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    McpSdkServerConfig,
    ResultMessage,
    TextBlock,
    query,
)
from claudia_core import Profile, Session
from claudia_harness import HarnessError, ProfileEditor, Turn, TurnResult

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
) -> ClaudeAgentOptions:
    allowed = list(profile.tools)
    mcp_servers: dict[str, Any] = {}
    if control is not None:
        allowed.extend(control_tool_names())
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


async def _single_prompt(text: str, turn: Turn) -> AsyncIterator[dict[str, Any]]:
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
    content.append({"type": "text", "text": text})
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


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
    ) -> TurnResult:
        if not profile.auth_token:
            raise HarnessError("not_connected")

        control = make_control_server(profile, editor) if profile.allow_self_control else None
        options = build_options(
            profile,
            session,
            config_root=self._config_root,
            scrub_env=self._scrub_env,
            control=control,
        )

        prompt: Any = turn.text
        if turn.images:
            prompt = _single_prompt(turn.text, turn)

        parts: list[str] = []
        result_text: str | None = None
        harness_session_id: str | None = None
        try:
            # query() is an async generator; aclosing stops the CLI subprocess if we bail out early.
            stream = cast(AsyncGenerator[Any, None], query(prompt=prompt, options=options))
            async with aclosing(stream) as messages:
                async for message in messages:
                    if isinstance(message, AssistantMessage):
                        parts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                    elif isinstance(message, ResultMessage):
                        harness_session_id = message.session_id
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
        except Exception as exc:
            log.exception("claude harness crashed for profile %s", profile.name)
            raise HarnessError("claude_crashed") from exc

        text = (result_text if result_text is not None else "".join(parts)).strip()
        return TurnResult(text=text or "(no reply)", harness_session_id=harness_session_id)
