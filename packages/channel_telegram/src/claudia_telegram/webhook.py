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
from aiogram.types import Audio, BufferedInputFile, Message, PhotoSize, Update, Voice
from claudia_channels import Activity, Orchestrator
from claudia_core import Attachment, InboundMessage, OutboundMessage
from fastapi import APIRouter, Header, HTTPException, Request, status

from claudia_telegram.markdown import to_telegram_html

log = logging.getLogger(__name__)

CHANNEL = "telegram"
TELEGRAM_MESSAGE_LIMIT = 4096
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
            voice = BufferedInputFile(message.audio, filename="reply.ogg")
            await self._bot.send_voice(self._chat_id, voice)
        for chunk in _chunks(message.text):
            await self._send_formatted(chunk)

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


async def _download(bot: Bot, file: Voice | Audio | PhotoSize) -> bytes:
    buffer = io.BytesIO()
    await bot.download(file, destination=buffer)
    return buffer.getvalue()


async def _to_inbound(bot: Bot, message: Message) -> InboundMessage:
    attachments: list[Attachment] = []
    if message.voice is not None:
        audio = await _download(bot, message.voice)
        attachments.append(Attachment("audio", audio, "audio/ogg"))
    elif message.audio is not None:
        audio = await _download(bot, message.audio)
        mime = message.audio.mime_type or "audio/mpeg"
        attachments.append(Attachment("audio", audio, mime))
    if message.photo:
        image = await _download(bot, message.photo[-1])
        attachments.append(Attachment("image", image, "image/jpeg"))

    sender = message.from_user
    return InboundMessage(
        channel=CHANNEL,
        sender_id=str(sender.id) if sender else "",
        text=message.text or message.caption,
        attachments=tuple(attachments),
    )


def _chunks(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return [text]
    return [
        text[i : i + TELEGRAM_MESSAGE_LIMIT] for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)
    ]
