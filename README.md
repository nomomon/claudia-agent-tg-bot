# claudia

A personal agent you talk to through Telegram. Each person gets their own Claude subscription,
folder, and conversation. The Claude Code harness does the work. The bot only routes messages.

## Layout

```
apps/bot/            composition root: settings, config loading, webhook server, CLI
packages/core/       domain types (Principal, Profile, Session, messages). No I/O.
packages/harness/    Harness port: the contract every agent backend implements
packages/harness_claude/  Claude Code adapter (claude-agent-sdk) + self-control tools
packages/channels/   channel-neutral orchestrator: routing, isolation, voice commands
packages/channel_telegram/  Telegram adapter: webhook in, replies and chat actions out
packages/store/      SQLite persistence with versioned migrations
packages/media/      speech in (Whisper) and speech out (OpenAI TTS, Ogg Opus)
config/bot.example.toml  people, profiles, models. Copy to config/bot.toml.
tests/               orchestrator, store, command parsing, and Claude option tests
```

Dependencies point inward. `channels` never imports a harness or a channel adapter. Only
`apps/bot` knows every adapter. Adding Codex or Slack means one new package and one line in
`container.py`.

## Behaviour

- **Isolation.** Every message is resolved to a principal first. Profiles, sessions, login
  tokens, and config dirs are all looked up through that principal.
- **Sessions.** One active session per profile. Replies resume it. Starting a new one archives
  the old one and keeps it in the database.
- **Voice commands.** Short voice messages like "start a new session" or "reset" are handled
  before the agent sees them, and get a spoken confirmation. Typed `/new` and `/status` also work.
- **Voice replies.** A profile with `voice_reply = true` answers with audio plus text.
- **Self-control.** Profiles with `allow_self_control = true` get two tools: `set_model`
  (limited to the profile's allowlist) and `show_profile`.
- **Harness secrets.** Bot secrets are blanked in the Claude subprocess environment, and each
  profile gets its own `CLAUDE_CONFIG_DIR` and subscription token.

## Local setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env            # fill in the token and secret
cp config/bot.example.toml config/bot.toml   # fill in Telegram user ids and folders
uv run pytest
uv run claudia serve            # then point a webhook at it (see below)
```

Telegram needs an HTTPS URL, so for local testing use a tunnel (for example `cloudflared` or
`ngrok`) and set `PUBLIC_BASE_URL` to its address.

## Connecting a Claude subscription

Each profile needs its own login. The person who owns that subscription should run this:

```bash
uv run claudia auth claude --profile sister
```

It runs `claude setup-token`, then asks you to paste the printed token. The token is stored in
the database against that profile only.

## Telegram webhook

```bash
uv run claudia webhook set      # registers PUBLIC_BASE_URL + webhook path with the secret
uv run claudia webhook info     # check url and last error
```

## Deploying with Dokploy

Same setup as the rat bot: Dokploy builds `docker-compose.yml` from this repo.

1. In Dokploy, create a Docker Compose app pointing at this repo and branch.
2. Set the environment variables from `.env.example`. For `BOT_CONFIG_TOML`, paste the full
   contents of your filled-in `config/bot.toml`. That file stays out of git.
3. Add a domain in Dokploy for the `bot` service, on container port `8000`. Dokploy issues TLS.
   Set `PUBLIC_BASE_URL` to that domain.
4. Deploy. Then register the webhook from a shell in the container:
   `claudia webhook set`
5. Connect each profile's Claude login from the same shell:
   `claudia auth claude --profile admin`, and the same for `sister` (she runs it, or you do it with her).

Data lives in the `claudia_data` volume (database and Claude config dirs). Per-person folders
live in `claudia_work`. Back up `claudia_data`.

## Known limits (MVP)

- The `sister` profile has no tools, so it is chat only. Anything file-based needs a new tool
  policy.
- The `admin` profile can read the bot's `.env` and database, because its working folder is the
  repo. That is fine for you as the only admin, but do not give other people admin.
- On Dokploy the admin profile's working folder is the Dokploy checkout of this repo.
- The Claude subprocess runs in the same container as the bot. The env is scrubbed, but the
  bot's files are still on disk. A stronger split (separate user or container per profile) is
  the next step if more people join.
- Long turns hold the chat's typing indicator until the reply is ready. There is no streaming yet.
- Restarting the bot from its own chat is not implemented.
