# ruff: noqa: E501  (catalog strings are long on purpose)
"""Every message the bot writes on its own, in each supported language.

Agent replies are not here: they come from the model in whatever language it is given.
Add a language by adding a column to every entry. Missing keys fail loudly in tests.
"""

from __future__ import annotations

from string import Template

SUPPORTED_LANGUAGES = ("en", "ru")

_MESSAGES: dict[str, dict[str, str]] = {
    "no_profile": {
        "en": "You don't have a profile set up yet.",
        "ru": "У вас пока нет профиля.",
    },
    "voice_off": {
        "en": "Voice messages are not enabled on this bot.",
        "ru": "Голосовые сообщения на этом боте не включены.",
    },
    "voice_failed": {
        "en": "I couldn't make out that voice message. Please try again or type it.",
        "ru": "Не получилось разобрать голосовое сообщение. Попробуйте ещё раз или напишите текстом.",
    },
    "voice_quota": {
        "en": "Voice messages are paused: the OpenAI quota has run out. Top up the billing and try again.",
        "ru": "Голосовые сообщения временно недоступны: закончилась квота OpenAI. Пополните баланс и попробуйте снова.",
    },
    "new_session": {
        "en": "Started a new session.",
        "ru": "Начали новый разговор.",
    },
    "status": {
        "en": "Profile: $name\nModel: $model\nVoice replies: $voice",
        "ru": "Профиль: $name\nМодель: $model\nГолосовые ответы: $voice",
    },
    "on": {"en": "on", "ru": "включены"},
    "off": {"en": "off", "ru": "выключены"},
    "default_model": {"en": "default", "ru": "по умолчанию"},
    "image_only": {"en": "(sent an image)", "ru": "(отправлено изображение)"},
    "no_reply": {"en": "(no reply)", "ru": "(нет ответа)"},
    "not_connected": {
        "en": "This profile is not connected to Claude yet.",
        "ru": "Этот профиль ещё не подключён к Claude.",
    },
    "no_harness": {
        "en": "This profile's assistant is not available.",
        "ru": "Помощник для этого профиля сейчас недоступен.",
    },
    "claude_login_invalid": {
        "en": "The Claude login for this bot has expired. Ask the admin to reconnect it.",
        "ru": "Вход в Claude для этого бота истёк. Попросите администратора подключить его заново.",
    },
    "claude_rate_limited": {
        "en": "Claude has hit its usage limit for now. Please try again a bit later.",
        "ru": "Лимит использования Claude на сейчас исчерпан. Попробуйте чуть позже.",
    },
    "claude_overloaded": {
        "en": "Claude is busy right now. Please try again in a minute.",
        "ru": "Claude сейчас перегружен. Попробуйте через минуту.",
    },
    "claude_failed": {
        "en": "Claude could not answer. Please try again.",
        "ru": "Claude не смог ответить. Попробуйте ещё раз.",
    },
    "claude_crashed": {
        "en": "Something went wrong talking to Claude.",
        "ru": "Что-то пошло не так при разговоре с Claude.",
    },
}


def text(key: str, language: str, **values: str) -> str:
    """Return the message for `key` in `language`, falling back to English."""
    entry = _MESSAGES[key]
    template = entry.get(language, entry["en"])
    return Template(template).substitute(values)
