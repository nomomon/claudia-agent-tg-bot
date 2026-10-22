import pytest
from claudia_channels import Command, parse_command


@pytest.mark.parametrize(
    "text",
    [
        "start a new",
        "Start a new session.",
        "please start a new chat",
        "new conversation",
        "begin over",
        "reset",
        "Reset the chat",
        "open a fresh session please",
    ],
)
def test_voice_new_session_phrases(text: str) -> None:
    assert parse_command(text, from_voice=True) is Command.NEW_SESSION


@pytest.mark.parametrize(
    "text",
    [
        "start a new project about cats for my nephew",  # real request, too long and off-pattern
        "what is a new york style pizza",
        "I need help with my new chat app",
        "",
    ],
)
def test_voice_questions_are_not_commands(text: str) -> None:
    assert parse_command(text, from_voice=True) is None


def test_typed_commands_are_exact() -> None:
    assert parse_command("/new", from_voice=False) is Command.NEW_SESSION
    assert parse_command("/STATUS", from_voice=False) is Command.STATUS
    assert parse_command("start a new", from_voice=False) is None
