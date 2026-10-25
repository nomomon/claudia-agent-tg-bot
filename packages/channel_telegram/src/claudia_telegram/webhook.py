from __future__ import annotations

import asyncio
import hmac
import io
import logging
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BufferedInputFile, Message, Update
from aiogram.types.downloadable import Downloadable
from claudia_channels import Activity, Orchestrator
from claudia_core import Attachment, AttachmentKind, InboundMessage, OutboundMessage
from fastapi import APIRouter, Header, HTTPException, Request, status

from claudia_telegram.markdown import to_telegram_html

log = logging.getLogger(__name__)

CHANNEL = "telegram"
TELEGRAM_MESSAGE_LIMIT = 4096
CAPTION_TEXT_LIMIT = 900
ACTION_REFRESH_SECONDS = 4

_ACTIONS: dict[Activity, ChatAction] = {
    "typing": ChatAction.TYPING,
    "recording": ChatAction.RECORD_VOICE,
}


class TelegramSink:
    """Replies for one chat. Keeps the typing or recording indicator alive while busy."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._pulse: asyncio.Task[None] | None = None

    async def activity(self, kind: Activity) -> None:
        await self._stop_pulse()
        if kind == "idle":
            return
        self._pulse = asyncio.create_task(self._keep_alive(_ACTIONS[kind]))

    async def send(self, message: OutboundMessage) -> None:
        await self._stop_pulse()
        if message.audio:
            # One message: the voice note with the text in an expandable caption.
            await self._send_voice(message.audio, message.text)
            return
        for chunk in _chunks(message.text):
            await self._send_formatted(chunk)

    async def _send_voice(self, audio: bytes, text: str) -> None:
        voice = BufferedInputFile(audio, filename="reply.ogg")
        try:
            await self._bot.send_voice(
                self._chat_id, voice, caption=voice_caption(text), parse_mode="HTML"
            )
        except TelegramBadRequest:
            log.warning("telegram rejected voice caption, sending plain caption")
            await self._bot.send_voice(self._chat_id, voice, caption=text[:CAPTION_TEXT_LIMIT])

    async def _send_formatted(self, markdown: str) -> None:
        """Send as Telegram HTML. If Telegram rejects the markup, send the plain text."""
        try:
            await self._bot.send_message(
                self._chat_id, to_telegram_html(markdown), parse_mode="HTML"
            )
        except TelegramBadRequest:
            log.warning("telegram rejected formatted reply, sending plain text")
            await self._bot.send_message(self._chat_id, markdown)

    async def _keep_alive(self, action: ChatAction) -> None:
        while True:
            try:
                await self._bot.send_chat_action(self._chat_id, action)
            except TelegramAPIError:
                log.warning("could not send chat action to %s", self._chat_id)
                return
            await asyncio.sleep(ACTION_REFRESH_SECONDS)

    async def _stop_pulse(self) -> None:
        if self._pulse is None:
            return
        self._pulse.cancel()
        with suppress(asyncio.CancelledError):
            await self._pulse
        self._pulse = None


class TelegramChannel:
    def __init__(self, *, bot: Bot, orchestrator: Orchestrator, webhook_secret: str) -> None:
        self._bot = bot
        self._orchestrator = orchestrator
        self._secret = webhook_secret
        # Hold references so background turns are not garbage collected mid-flight.
        self._tasks: set[asyncio.Task[None]] = set()

    def router(self, path: str) -> APIRouter:
        router = APIRouter()

        @router.post(path, status_code=status.HTTP_200_OK)
        async def receive(
            request: Request,
            secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
        ) -> dict[str, bool]:
            if not secret or not hmac.compare_digest(secret, self._secret):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
            update = Update.model_validate(await request.json(), context={"bot": self._bot})
            # Acknowledge fast. Telegram retries slow webhooks, and a turn can take minutes.
            self._spawn(self.process(update))
            return {"ok": True}

        return router

    async def process(self, update: Update) -> None:
        message = update.message
        if message is None or message.from_user is None:
            return
        try:
            inbound = await _to_inbound(self._bot, message)
            await self._orchestrator.handle(inbound, TelegramSink(self._bot, message.chat.id))
        except Exception:
            log.exception("failed to handle telegram update %s", update.update_id)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def _download(bot: Bot, file: Downloadable) -> bytes:
    """Fetch a file from Telegram. Bots can only download files up to 20 MB.

    Returns empty bytes when Telegram refuses, so the orchestrator can say why.
    """
    buffer = io.BytesIO()
    try:
        await bot.download(file, destination=buffer)
    except TelegramBadRequest:
        log.warning("telegram would not download a file (probably over 20 MB)")
        return b""
    return buffer.getvalue()


async def _attach(bot: Bot, message: Message) -> list[Attachment]:
    """Every media type Telegram sends, mapped to a kind and mime type the bot understands."""
    found: list[Attachment] = []

    async def add(kind: AttachmentKind, obj: Downloadable, mime: str, name: str | None) -> None:
        found.append(Attachment(kind, await _download(bot, obj), mime, name))

    if message.voice is not None:
        await add("audio", message.voice, message.voice.mime_type or "audio/ogg", None)
    if message.audio is not None:
        name = message.audio.file_name
        await add("audio", message.audio, message.audio.mime_type or "audio/mpeg", name)
    if message.photo:
        await add("image", message.photo[-1], "image/jpeg", None)
    if message.document is not None:
        doc = message.document
        await add("file", doc, doc.mime_type or "application/octet-stream", doc.file_name)
    if message.video is not None:
        video = message.video
        await add("video", video, video.mime_type or "video/mp4", video.file_name)
    if message.video_note is not None:
        await add("video", message.video_note, "video/mp4", None)
    if message.animation is not None:
        anim = message.animation
        await add("video", anim, anim.mime_type or "video/mp4", anim.file_name)
    if message.sticker is not None:
        sticker = message.sticker
        if sticker.is_video:
            await add("video", sticker, "video/webm", None)
        elif sticker.is_animated:
            await add("file", sticker, "application/x-tgsticker", "sticker.tgs")
        else:
            await add("image", sticker, "image/webp", None)
    return found


async def _to_inbound(bot: Bot, message: Message) -> InboundMessage:
    sender = message.from_user
    return InboundMessage(
        channel=CHANNEL,
        sender_id=str(sender.id) if sender else "",
        text=message.text or message.caption,
        attachments=tuple(await _attach(bot, message)),
    )


def voice_caption(text: str) -> str:
    """The reply text as an expandable quote. Telegram shows the first lines and folds the rest.

    Captions are capped at 1024 characters, so long replies are cut here. The full text is
    not lost from the model's side, only from this single message.
    """
    shown = text if len(text) <= CAPTION_TEXT_LIMIT else text[:CAPTION_TEXT_LIMIT] + "…"
    return f"<blockquote expandable>{to_telegram_html(shown)}</blockquote>"


def _chunks(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return [text]
    return [
        text[i : i + TELEGRAM_MESSAGE_LIMIT] for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)
    ]
