from pathlib import Path

from claudia_core import Profile, Session
from claudia_harness_claude import build_options
from claudia_harness_claude.control import control_tool_names
from claudia_harness_claude.harness import BUILTIN_TOOLS

SECRETS = ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY")


def _profile(name: str, *, tools: tuple[str, ...], token: str | None) -> Profile:
    return Profile(
        name=name,
        principal_id=name,
        harness="claude",
        working_dir=f"/work/{name}",
        tools=tools,
        auth_token=token,
    )


def _session(harness_session_id: str | None = None) -> Session:
    return Session(id=1, profile_name="x", harness_session_id=harness_session_id, status="active")


def test_bot_secrets_are_blanked_for_the_harness_process() -> None:
    options = build_options(
        _profile("sister", tools=(), token="t-sister"),
        _session(),
        config_root=Path("/data/claude"),
        scrub_env=SECRETS,
        control=None,
    )
    assert options.env["TELEGRAM_BOT_TOKEN"] == ""
    assert options.env["OPENAI_API_KEY"] == ""


def test_each_profile_gets_its_own_login_and_config_dir() -> None:
    config_root = Path("/data/claude")
    sister = build_options(
        _profile("sister", tools=(), token="t-sister"),
        _session(),
        config_root=config_root,
        scrub_env=SECRETS,
        control=None,
    )
    admin = build_options(
        _profile("admin", tools=("Read",), token="t-admin"),
        _session(),
        config_root=config_root,
        scrub_env=SECRETS,
        control=None,
    )
    assert sister.env["CLAUDE_CONFIG_DIR"] == "/data/claude/sister"
    assert sister.env["CLAUDE_CODE_OAUTH_TOKEN"] == "t-sister"
    assert admin.env["CLAUDE_CONFIG_DIR"] == "/data/claude/admin"
    assert admin.env["CLAUDE_CODE_OAUTH_TOKEN"] == "t-admin"
    assert "t-admin" not in sister.env.values()
    assert "t-sister" not in admin.env.values()


def test_profile_without_tools_disallows_every_builtin() -> None:
    options = build_options(
        _profile("sister", tools=(), token="t"),
        _session(),
        config_root=Path("/data/claude"),
        scrub_env=SECRETS,
        control=None,
    )
    assert options.allowed_tools == []
    assert set(options.disallowed_tools) == set(BUILTIN_TOOLS)


def test_granted_tools_are_allowed_and_the_rest_disallowed() -> None:
    options = build_options(
        _profile("admin", tools=("Read", "Edit"), token="t"),
        _session(),
        config_root=Path("/data/claude"),
        scrub_env=SECRETS,
        control=None,
    )
    assert options.allowed_tools == ["Read", "Edit"]
    assert "Read" not in options.disallowed_tools
    assert "Bash" in options.disallowed_tools
    assert options.permission_mode == "dontAsk"


def test_control_tools_only_when_a_control_server_is_passed() -> None:
    server = object()  # stands in for an McpSdkServerConfig
    options = build_options(
        _profile("admin", tools=("Read",), token="t"),
        _session(),
        config_root=Path("/data/claude"),
        scrub_env=SECRETS,
        control=server,  # type: ignore[arg-type]
    )
    assert set(control_tool_names()) <= set(options.allowed_tools)
    assert isinstance(options.mcp_servers, dict)
    assert options.mcp_servers["claudia"] is server


def test_resume_uses_the_stored_harness_session() -> None:
    options = build_options(
        _profile("admin", tools=("Read",), token="t"),
        _session("hs-42"),
        config_root=Path("/data/claude"),
        scrub_env=SECRETS,
        control=None,
    )
    assert options.resume == "hs-42"
