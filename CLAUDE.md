# Claudia

Personal agent bot. Telegram webhook in, Claude Code harness out. Each person gets their own profile, subscription login, working folder, and session.

## Layout

- `packages/core`: domain types and errors. No I/O.
- `packages/harness`: harness port. `harness_claude` is the Claude Code adapter.
- `packages/channels`: channel-neutral orchestrator, commands, and bot messages (`messages.py`, English and Russian).
- `packages/channel_telegram`: Telegram adapter only.
- `packages/store`: SQLite with numbered migrations. Append, never edit a shipped one.
- `packages/media`: Whisper transcription and Gemini speech.
- `apps/bot`: composition root, config loading, CLI.

Dependencies point inward. Channels and harnesses never import each other.

## Conventions

- Python 3.12+, uv workspace. Checks: `uv run ruff check .`, `uv run pyright packages apps tests`, `uv run pytest`.
- Commits use Conventional Commits, straight to `main`.
- Secrets and `config/bot.toml` stay out of git.
