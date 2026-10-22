from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claudia_core import Principal, Profile, Role
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Secrets the bot itself reads from its environment. Harness subprocesses inherit the
# environment, so these are blanked for them. See claudia_harness_claude.build_env.
SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr
    public_base_url: str = ""
    webhook_path: str = "/telegram/webhook"
    openai_api_key: SecretStr | None = None

    data_dir: Path = Path("data")
    # Full bot.toml content. Set this on hosts where the file is not in the repo (Dokploy).
    # When empty, config_path is read from disk instead.
    bot_config_toml: str | None = None
    config_path: Path = Path("config/bot.toml")
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def database_path(self) -> Path:
        return self.data_dir / "claudia.sqlite3"

    @property
    def claude_config_root(self) -> Path:
        return self.data_dir / "claude"


@dataclass(frozen=True)
class PrincipalConfig:
    principal: Principal
    telegram_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BotConfig:
    principals: tuple[PrincipalConfig, ...]
    profiles: tuple[Profile, ...]
    identities: list[tuple[str, str, str]] = field(default_factory=list)


def load_bot_config(settings: Settings) -> BotConfig:
    if settings.bot_config_toml:
        return parse_bot_config(tomllib.loads(settings.bot_config_toml))
    return parse_bot_config(tomllib.loads(settings.config_path.read_text(encoding="utf-8")))


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
            raise ValueError(f"profile '{entry['name']}' points at unknown principal '{owner}'")
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
