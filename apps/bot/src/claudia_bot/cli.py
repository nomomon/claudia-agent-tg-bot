"""Operator commands: serve, connect a Claude login, manage the Telegram webhook."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from aiogram import Bot

from claudia_bot.config import Settings
from claudia_bot.container import open_store


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from env / .env


def cmd_serve(_: argparse.Namespace) -> int:
    import uvicorn

    settings = _settings()
    uvicorn.run(
        "claudia_bot.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )
    return 0


def cmd_auth_claude(args: argparse.Namespace) -> int:
    """Store a Claude subscription token against one profile.

    Get the token with `claude setup-token` on any machine logged in as the subscription
    owner. The token is kept in the bot's database and only passed to that profile's
    harness process.
    """
    settings = _settings()
    store, _ = open_store(settings)
    try:
        store.load_profile(args.profile)
    except KeyError:
        print(
            f"No profile named '{args.profile}' in {settings.editable_config_path}.",
            file=sys.stderr,
        )
        return 1

    raw = args.token if args.token is not None else getpass.getpass("Token (input hidden): ")
    # Pasted tokens often pick up spaces or line breaks. A token never contains whitespace.
    token = "".join(raw.split())
    if not token.startswith("sk-ant-"):
        print("That does not look like a Claude token (expected it to start with sk-ant-).")
        return 1

    store.set_auth_token(args.profile, token)
    print(f"Stored a {len(token)}-character token for profile '{args.profile}'.")
    return 0


def _webhook_url(settings: Settings) -> str:
    if not settings.public_base_url:
        raise SystemExit("PUBLIC_BASE_URL is not set.")
    return settings.public_base_url.rstrip("/") + settings.webhook_path


async def _webhook(action: str, settings: Settings) -> None:
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    try:
        if action == "set":
            ok = await bot.set_webhook(
                url=_webhook_url(settings),
                secret_token=settings.telegram_webhook_secret.get_secret_value(),
                allowed_updates=["message"],
                drop_pending_updates=True,
            )
            print("Webhook set." if ok else "Telegram refused the webhook.")
        elif action == "delete":
            await bot.delete_webhook(drop_pending_updates=True)
            print("Webhook deleted.")
        else:
            info = await bot.get_webhook_info()
            print(f"url: {info.url or '(none)'}")
            print(f"pending updates: {info.pending_update_count}")
            print(f"last error: {info.last_error_message or '(none)'}")
    finally:
        await bot.session.close()


def cmd_webhook(args: argparse.Namespace) -> int:
    asyncio.run(_webhook(args.action, _settings()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claudia", description="Personal agent bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="Run the webhook server").set_defaults(func=cmd_serve)

    auth = sub.add_parser("auth", help="Connect a harness login")
    auth_sub = auth.add_subparsers(dest="harness", required=True)
    claude = auth_sub.add_parser("claude", help="Connect a Claude subscription to a profile")
    claude.add_argument("--profile", required=True, help="Profile name from bot.toml")
    claude.add_argument("--token", help="Token from `claude setup-token`. Prompts if omitted.")
    claude.set_defaults(func=cmd_auth_claude)

    webhook = sub.add_parser("webhook", help="Manage the Telegram webhook")
    webhook.add_argument("action", choices=["set", "info", "delete"])
    webhook.set_defaults(func=cmd_webhook)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
