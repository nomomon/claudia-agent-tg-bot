"""In-process MCP tools that let the agent change its own profile (for example, its model).

Only profiles with allow_self_control get these. Values are checked against the profile's
allowlist, and the write goes through the ProfileEditor the orchestrator provides.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from claudia_core import Profile
from claudia_harness import ProfileEditor

CONTROL_SERVER = "claudia"


def control_tool_names() -> list[str]:
    return [f"mcp__{CONTROL_SERVER}__{name}" for name in ("set_model", "show_profile")]


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def make_control_server(profile: Profile, editor: ProfileEditor) -> McpSdkServerConfig:
    allowed_models = ", ".join(profile.models) or "none configured"

    @tool(
        "set_model",
        f"Switch the model for future messages on this profile. Allowed models: {allowed_models}.",
        {"model": str},
    )
    async def set_model(args: dict[str, Any]) -> dict[str, Any]:
        model = args["model"]
        if model not in profile.models:
            return _text(f"Not allowed. Allowed models: {allowed_models}.")
        await editor.set_model(profile.name, model)
        return _text(f"Model set to {model}. It applies from the next message.")

    @tool("show_profile", "Show the current model and working folder for this profile.", {})
    async def show_profile(args: dict[str, Any]) -> dict[str, Any]:
        current = await editor.get_profile(profile.name)
        return _text(
            f"Profile: {current.name}\nModel: {current.model or 'default'}\n"
            f"Working folder: {current.working_dir}"
        )

    return create_sdk_mcp_server(
        name=CONTROL_SERVER,
        version="0.1.0",
        tools=[set_model, show_profile],
    )
