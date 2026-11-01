"""In-process MCP tools the agent can call to act on the bot itself.

- Self-control (allow_self_control): change its own model, see its profile.
- Outbox (allow_outbox): send a message or a file to the person while the turn runs.
- Voice (voice_reply): speak text as voice notes when the person asks for audio.
- Messenger (message_targets): hand a message to another profile's own agent, in its own chat.
- Scheduler (allow_scheduling): wake itself up later, once or on a cron schedule.

Each tool is only offered to profiles that have the matching flag. Values are checked here,
and the actual write or send goes through the ProfileEditor and Outbox the orchestrator gives.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from claudia_core import OutgoingFile, Profile, SchedulerError
from claudia_harness import Messenger, Outbox, ProfileEditor, Scheduler

CONTROL_SERVER = "claudia"

# Telegram accepts bot uploads up to 50 MB.
MAX_FILE_BYTES = 50 * 1024 * 1024

# How long a get_current_time reading stays valid for schedule_task.
TIME_CHECK_MAX_AGE_SECONDS = 60

SELF_CONTROL_TOOLS = ("set_model", "show_profile")
OUTBOX_TOOLS = ("send_message", "send_file")
VOICE_TOOLS = ("send_voice",)
MESSENGER_TOOLS = ("message_user",)
SCHEDULER_TOOLS = (
    "get_current_time",
    "schedule_task",
    "list_scheduled_tasks",
    "delete_scheduled_task",
)


def control_tool_names(
    *,
    self_control: bool = True,
    outbox: bool = True,
    voice: bool = False,
    messenger: bool = False,
    scheduler: bool = False,
) -> list[str]:
    names: list[str] = []
    if self_control:
        names.extend(SELF_CONTROL_TOOLS)
    if outbox:
        names.extend(OUTBOX_TOOLS)
    if voice:
        names.extend(VOICE_TOOLS)
    if messenger:
        names.extend(MESSENGER_TOOLS)
    if scheduler:
        names.extend(SCHEDULER_TOOLS)
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
    messenger: Messenger | None = None,
    scheduler: Scheduler | None = None,
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

    if profile.message_targets and messenger is not None:
        targets_desc = ", ".join(profile.message_targets)

        @tool(
            "message_user",
            "Send a message to another person's own agent on this bot. It is delivered into "
            "their own chat and their agent may act on it and reply there, same as a message "
            f"from them. Allowed targets (by profile name): {targets_desc}.",
            {"to": str, "text": str},
        )
        async def message_user(args: dict[str, Any]) -> dict[str, Any]:
            to = args["to"]
            if to not in profile.message_targets:
                return _text(f"Not allowed. Allowed targets: {targets_desc}.")
            result = await messenger.send_to(to, args["text"])
            return _text(result)

        tools.append(message_user)

    if profile.allow_scheduling and scheduler is not None:
        last_time_check: datetime | None = None

        @tool(
            "get_current_time",
            "Read the current date and time. Call this whenever the exact time matters: before "
            "scheduling anything, when asked what day or time it is, or to turn a relative "
            "time ('tomorrow at nine', 'in two hours') into a concrete one. The time in your "
            "instructions is only a snapshot from when this turn started and goes stale while "
            f"you work, so never compute a schedule from it alone. schedule_task refuses to run "
            f"unless this was called within the last {TIME_CHECK_MAX_AGE_SECONDS} seconds.",
            {},
        )
        async def get_current_time(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal last_time_check
            now = scheduler.now()
            last_time_check = now
            return _text(f"{_format_moment(now)}. ISO: {now.isoformat(timespec='seconds')}")

        @tool(
            "schedule_task",
            "Schedule yourself to be woken up later to do something. When it fires, you are "
            "started again in this same chat with a message containing prompt verbatim, plus "
            "your usual recent memory. Nothing is sent to the person unless you call "
            "send_message during that run, so write prompt as a note to your future self: what "
            "to do, and any context you will need. Provide exactly one of 'at' (one-off, "
            "ISO-8601 local time, e.g. '2026-09-10 09:30') or 'cron' (repeating, standard "
            "5-field cron 'minute hour day-of-month month day-of-week', e.g. '0 9 * * *' daily "
            "at 09:00, '*/30 * * * *' every 30 minutes). Call get_current_time first and "
            "compute the time from what it returns; leave the other one empty.",
            {"prompt": str, "title": str, "at": str, "cron": str},
        )
        async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
            stale = last_time_check is None or (
                scheduler.now() - last_time_check
            ).total_seconds() > TIME_CHECK_MAX_AGE_SECONDS
            if stale:
                return _text(
                    "Stale clock: call get_current_time first (the reading must be less than "
                    f"{TIME_CHECK_MAX_AGE_SECONDS} seconds old), recompute the time from what "
                    "it returns, then call this tool again."
                )
            try:
                job = await scheduler.schedule(
                    prompt=args["prompt"],
                    title=args["title"],
                    at=args.get("at") or None,
                    cron=args.get("cron") or None,
                )
            except SchedulerError as exc:
                return _text(str(exc))
            return _text(
                f'Scheduled task id={job.id} "{job.title}" ({job.schedule_description}). '
                f"Next run: {job.next_run_at:%Y-%m-%d %H:%M}."
            )

        @tool(
            "list_scheduled_tasks",
            "List your scheduled tasks with their ids, schedules and prompts. Call this before "
            "deleting one (you need its id), or whenever asked what is scheduled.",
            {},
        )
        async def list_scheduled_tasks(args: dict[str, Any]) -> dict[str, Any]:
            jobs = await scheduler.list_tasks()
            if not jobs:
                return _text("No scheduled tasks.")
            lines = [
                f"- id={j.id} | {j.title} | {j.schedule_description} | "
                f"next: {j.next_run_at:%Y-%m-%d %H:%M} | prompt: {j.prompt}"
                for j in jobs
            ]
            return _text("\n".join(lines))

        @tool(
            "delete_scheduled_task",
            "Delete one of your scheduled tasks by id. Use list_scheduled_tasks first to find "
            "the id.",
            {"task_id": str},
        )
        async def delete_scheduled_task(args: dict[str, Any]) -> dict[str, Any]:
            task_id = args["task_id"]
            if not await scheduler.delete(task_id):
                return _text(f"No scheduled task with id={task_id}.")
            return _text(f"Deleted scheduled task id={task_id}.")

        tools.extend(
            [get_current_time, schedule_task, list_scheduled_tasks, delete_scheduled_task]
        )

    return create_sdk_mcp_server(name=CONTROL_SERVER, version="0.2.0", tools=tools)


def _format_moment(moment: datetime) -> str:
    """Render a moment for the model: date, time, weekday and timezone."""
    return f"{moment:%Y-%m-%d %H:%M %A}, timezone {moment.tzinfo} (UTC{moment:%z})"


def _guess_mime(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
