"""Wiring. The only module that knows every adapter."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from claudia_channels import Orchestrator
from claudia_harness import HarnessRegistry
from claudia_harness_claude import ClaudeCodeHarness
from claudia_media import OpenAISpeech
from claudia_store import SqliteStore
from claudia_telegram import TelegramChannel
from openai import AsyncOpenAI

from claudia_bot.config import SECRET_ENV_NAMES, BotConfig, Settings, load_bot_config


@dataclass
class Container:
    settings: Settings
    config: BotConfig
    store: SqliteStore
    bot: Bot
    channel: TelegramChannel


def open_store(settings: Settings) -> tuple[SqliteStore, BotConfig]:
    """Open the database and make it match bot.toml. Used by the server and the CLI."""
    config = load_bot_config(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(settings.database_path)
    store.sync_config(
        principals=[p.principal for p in config.principals],
        identities=config.identities,
        profiles=config.profiles,
    )
    return store, config


def build_container(settings: Settings) -> Container:
    store, config = open_store(settings)

    speech = None
    if settings.openai_api_key is not None:
        speech = OpenAISpeech(
            AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value()),
            tts_voice=settings.tts_voice,
        )

    harnesses = HarnessRegistry(
        {
            "claude": ClaudeCodeHarness(
                config_root=settings.claude_config_root,
                scrub_env=SECRET_ENV_NAMES,
            ),
        }
    )

    orchestrator = Orchestrator(
        repo=store,
        harnesses=harnesses,
        transcriber=speech,
        speaker=speech,
    )

    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    channel = TelegramChannel(
        bot=bot,
        orchestrator=orchestrator,
        webhook_secret=settings.telegram_webhook_secret.get_secret_value(),
    )
    return Container(settings=settings, config=config, store=store, bot=bot, channel=channel)
