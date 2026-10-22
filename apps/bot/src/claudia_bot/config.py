from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claudia_core import Principal, Profile, Role
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# Secrets the bot itself reads from its environment. Harness subprocesses inherit the
# environment, so these are blanked for them. See claudia_harness_claude.build_env.
SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

CONFIG_FILE_NAME = "bot.toml"


class ConfigError(Exception):
    """The bot config is missing or invalid. The message says where and why."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr
    public_base_url: str = ""
    webhook_path: str = "/telegram/webhook"
    openai_api_key: SecretStr | None = None

    data_dir: Path = Path("data")
    # Full bot.toml text. Used to create the editable copy in data_dir on first boot.
    bot_config_toml: str | None = None
    # Overwrite the editable copy in data_dir from bot_config_toml on this boot.
    bot_config_reseed: bool = False
    # Fallback for local runs when there is no editable copy and no env text.
    config_path: Path = Path("config/bot.toml")
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def database_path(self) -> Path:
        return self.data_dir / "claudia.sqlite3"

    @property
    def claude_config_root(self) -> Path:
        return self.data_dir / "claude"

    @property
    def editable_config_path(self) -> Path:
        return self.data_dir / CONFIG_FILE_NAME


@dataclass(frozen=True)
class PrincipalConfig:
    principal: Principal
    telegram_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BotConfig:
    principals: tuple[PrincipalConfig, ...]
    profiles: tuple[Profile, ...]
    identities: list[tuple[str, str, str]] = field(default_factory=list)


def read_config_text(settings: Settings) -> tuple[str, str]:
    """Return (toml text, where it came from).

    Order:
    1. The editable copy in data_dir. Edit this file to change the config later.
    2. BOT_CONFIG_TOML. Written to data_dir on first boot, or on every boot if
       BOT_CONFIG_RESEED is set. After that the env value is ignored.
    3. config_path on disk (local runs).
    """
    stored = settings.editable_config_path
    env_text = settings.bot_config_toml

    if env_text and (settings.bot_config_reseed or not stored.exists()):
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_text(env_text, encoding="utf-8")
        return env_text, f"BOT_CONFIG_TOML (saved to {stored})"
    if stored.exists():
        return stored.read_text(encoding="utf-8"), str(stored)
    if settings.config_path.exists():
        return settings.config_path.read_text(encoding="utf-8"), str(settings.config_path)
    raise ConfigError(
        f"No bot config found. Set BOT_CONFIG_TOML to the contents of bot.toml, or create {stored}."
    )


def load_bot_config(settings: Settings) -> BotConfig:
    text, source = read_config_text(settings)
    log.info(
        "config source: %s (%d bytes, %d lines)",
        source,
        len(text.encode("utf-8")),
        text.count("\n") + 1,
    )
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source} is not valid TOML: {exc}") from exc

    config = parse_bot_config(raw)
    log.info(
        "config loaded: principals=%s profiles=%s telegram_ids=%d",
        [p.principal.id for p in config.principals],
        [p.name for p in config.profiles],
        len(config.identities),
    )
    return config


def parse_bot_config(raw: dict[str, Any]) -> BotConfig:
    principals: list[PrincipalConfig] = []
    identities: list[tuple[str, str, str]] = []
    for entry in raw.get("principals", []):
        principal = Principal(id=entry["id"], name=entry["name"], role=Role(entry["role"]))
        telegram_ids = tuple(int(t) for t in entry.get("telegram_ids", []))
        principals.append(PrincipalConfig(principal=principal, telegram_ids=telegram_ids))
        identities.extend(("telegram", str(t), principal.id) for t in telegram_ids)

    known = {p.principal.id for p in principals}
    profiles: list[Profile] = []
    for entry in raw.get("profiles", []):
        owner = entry["principal"]
        if owner not in known:
            raise ConfigError(f"profile '{entry['name']}' points at unknown principal '{owner}'")
        profiles.append(
            Profile(
                name=entry["name"],
                principal_id=owner,
                harness=entry.get("harness", "claude"),
                working_dir=entry["working_dir"],
                tools=tuple(entry.get("tools", [])),
                model=entry.get("model"),
                models=tuple(entry.get("models", [])),
                allow_self_control=bool(entry.get("allow_self_control", False)),
                voice_reply=bool(entry.get("voice_reply", False)),
                system_prompt=entry.get("system_prompt"),
            )
        )

    return BotConfig(
        principals=tuple(principals),
        profiles=tuple(profiles),
        identities=identities,
    )
