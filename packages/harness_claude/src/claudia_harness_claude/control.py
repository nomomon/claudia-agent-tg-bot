"""In-process MCP tools the agent can call to act on the bot itself.

- Self-control (allow_self_control): change its own model, see its profile.
- Outbox (allow_outbox): send a message or a file to the person while the turn runs.
- Voice (voice_reply): speak text as voice notes when the person asks for audio.

Each tool is only offered to profiles that have the matching flag. Values are checked here,
and the actual write or send goes through the ProfileEditor and Outbox the orchestrator gives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from claudia_core import OutgoingFile, Profile
from claudia_harness import Outbox, ProfileEditor

CONTROL_SERVER = "claudia"

# Telegram accepts bot uploads up to 50 MB.
MAX_FILE_BYTES = 50 * 1024 * 1024

SELF_CONTROL_TOOLS = ("set_model", "show_profile")
OUTBOX_TOOLS = ("send_message", "send_file")
VOICE_TOOLS = ("send_voice",)


def control_tool_names(
    *, self_control: bool = True, outbox: bool = True, voice: bool = False
) -> list[str]:
    names: list[str] = []
    if self_control:
        names.extend(SELF_CONTROL_TOOLS)
    if outbox:
        names.extend(OUTBOX_TOOLS)
    if voice:
        names.extend(VOICE_TOOLS)
    return [f"mcp__{CONTROL_SERVER}__{name}" for name in names]


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def resolve_sendable(raw: str, roots: list[Path]) -> Path:
    """Find a file the agent may send. Raises ValueError with a reason the agent can read.

    The file must sit inside one of the profile's folders, must not be a dotfile or inside a
    dot-folder (so .env and .git never leave), and must fit Telegram's upload limit.
    """
    if not roots:
        raise ValueError("this profile has no folder to send files from")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    resolved = candidate.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("that file is outside the folders this profile may send from")
    if any(part.startswith(".") for part in resolved.parts):
        raise ValueError("dotfiles and dot-folders are never sent")
    if not resolved.is_file():
        raise ValueError("no file at that path")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("the file is over Telegram's 50 MB limit")
    return resolved


def make_control_server(
    profile: Profile,
    editor: ProfileEditor,
    *,
    outbox: Outbox | None,
    sendable_roots: list[Path],
    voice: bool = False,
) -> McpSdkServerConfig:
    tools: list[Any] = []

    if profile.allow_self_control:
        allowed_models = ", ".join(profile.models) or "none configured"

        @tool(
            "set_model",
            f"Switch the model for future messages on this profile. Allowed models: "
            f"{allowed_models}.",
            {"model": str},
        )
        async def set_model(args: dict[str, Any]) -> dict[str, Any]:
            model = args["model"]
            if model not in profile.models:
                return _text(f"Not allowed. Allowed models: {allowed_models}.")
            await editor.set_model(profile.name, model)
            return _text(f"Model set to {model}. It applies from the next message.")

        @tool("show_profile", "Show the current model and working folder for this profile.", {})
        async def show_profile(args: dict[str, Any]) -> dict[str, Any]:
            current = await editor.get_profile(profile.name)
            return _text(
                f"Profile: {current.name}\nModel: {current.model or 'default'}\n"
                f"Working folder: {current.working_dir}"
            )

        tools.extend([set_model, show_profile])

    if profile.allow_outbox and outbox is not None:

        @tool(
            "send_message",
            "Send a text message to the person right away, while you are still working. "
            "Your final answer is sent as well, so use this only for updates or for something "
            "they should see before you finish.",
            {"text": str},
        )
        async def send_message(args: dict[str, Any]) -> dict[str, Any]:
            await outbox.send_text(args["text"])
            return _text("Sent.")

        @tool(
            "send_file",
            "Send a file from this profile's folders to the person. kind is 'photo' for "
            "images (shown inline) or 'document' for anything else. path is absolute or "
            "relative to the working folder. caption is optional.",
            {"path": str, "kind": str, "caption": str},
        )
        async def send_file(args: dict[str, Any]) -> dict[str, Any]:
            kind = args.get("kind", "document")
            if kind not in ("photo", "document"):
                return _text("kind must be 'photo' or 'document'.")
            try:
                path = resolve_sendable(args["path"], sendable_roots)
            except ValueError as exc:
                return _text(f"Not sent: {exc}.")
            mime = _guess_mime(path)
            await outbox.send_file(
                OutgoingFile(
                    kind=kind,
                    name=path.name,
                    data=path.read_bytes(),
                    mime_type=mime,
                    caption=args.get("caption", ""),
                )
            )
            return _text(f"Sent {path.name}.")

        tools.extend([send_message, send_file])

    if voice and outbox is not None:

        @tool(
            "send_voice",
            "Send text to the person as voice notes. Use this only when they ask for audio or "
            "a voice message. Otherwise reply in text. Once voice notes are sent, your final "
            "answer is not sent as text, so the voice text must hold the whole answer.",
            {"text": str},
        )
        async def send_voice(args: dict[str, Any]) -> dict[str, Any]:
            if await outbox.send_voice(args["text"]):
                return _text("Sent as voice.")
            return _text("Could not make the voice notes. Your text answer will still be sent.")

        tools.append(send_voice)

    return create_sdk_mcp_server(name=CONTROL_SERVER, version="0.2.0", tools=tools)


def _guess_mime(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
