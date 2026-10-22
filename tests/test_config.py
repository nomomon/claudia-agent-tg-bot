from pathlib import Path

import pytest
from claudia_bot.config import ConfigError, Settings, load_bot_config
from pydantic import SecretStr

TOML = """
[[principals]]
id = "admin"
name = "Admin"
role = "admin"
telegram_ids = [111]

[[profiles]]
name = "admin"
principal = "admin"
working_dir = "/repo"
"""

OTHER_TOML = TOML.replace('name = "admin"\nprincipal', 'name = "renamed"\nprincipal')


def _settings(data_dir: Path, **overrides) -> Settings:
    values = {
        "telegram_bot_token": SecretStr("t"),
        "telegram_webhook_secret": SecretStr("s"),
        "data_dir": data_dir,
        "config_path": data_dir / "missing.toml",
    }
    values.update(overrides)
    return Settings(**values)


def test_env_text_seeds_the_editable_copy_on_first_boot(tmp_path: Path) -> None:
    settings = _settings(tmp_path, bot_config_toml=TOML)
    config = load_bot_config(settings)
    assert [p.name for p in config.profiles] == ["admin"]
    assert config.identities == [("telegram", "111", "admin")]
    assert (tmp_path / "bot.toml").read_text() == TOML


def test_editable_copy_wins_over_env_text_on_later_boots(tmp_path: Path) -> None:
    (tmp_path / "bot.toml").write_text(OTHER_TOML)
    settings = _settings(tmp_path, bot_config_toml=TOML)
    assert [p.name for p in load_bot_config(settings).profiles] == ["renamed"]
    assert (tmp_path / "bot.toml").read_text() == OTHER_TOML


def test_reseed_flag_overwrites_editable_copy(tmp_path: Path) -> None:
    (tmp_path / "bot.toml").write_text(OTHER_TOML)
    settings = _settings(tmp_path, bot_config_toml=TOML, bot_config_reseed=True)
    assert [p.name for p in load_bot_config(settings).profiles] == ["admin"]


def test_invalid_toml_names_the_source_and_line(tmp_path: Path) -> None:
    settings = _settings(tmp_path, bot_config_toml="[[principals]\nid = 'x")
    with pytest.raises(ConfigError, match="is not valid TOML"):
        load_bot_config(settings)


def test_missing_config_gives_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="No bot config found"):
        load_bot_config(_settings(tmp_path))


def test_empty_env_values_are_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_CONFIG_RESEED", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_CONFIG_TOML", "")
    assert Settings(_env_file=None).bot_config_reseed is False  # type: ignore[call-arg]
