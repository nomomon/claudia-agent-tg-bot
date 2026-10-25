import pytest
from claudia_channels import messages


def test_every_message_has_english_and_russian() -> None:
    for key, entry in messages._MESSAGES.items():
        assert set(entry) >= {"en", "ru"}, key


def test_unknown_language_falls_back_to_english() -> None:
    assert messages.text("no_profile", "de") == messages.text("no_profile", "en")


def test_status_fills_placeholders_in_both_languages() -> None:
    en = messages.text("status", "en", name="x", model="m", voice="on")
    ru = messages.text("status", "ru", name="x", model="m", voice="включены")
    assert en == "Profile: x\nModel: m\nVoice replies: on"
    assert ru.endswith("Голосовые ответы: включены")


def test_missing_key_fails_loudly() -> None:
    with pytest.raises(KeyError):
        messages.text("no_such_key", "en")
