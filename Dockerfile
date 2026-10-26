# Build context is the repo root. The claude CLI ships inside the claude-agent-sdk wheel,
# so no Node toolchain is needed.
FROM python:3.13-slim

# ffmpeg encodes spoken replies to Ogg Opus for Telegram voice notes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first, so code edits do not reinstall everything.
COPY pyproject.toml uv.lock ./
COPY apps/bot/pyproject.toml apps/bot/
COPY packages/core/pyproject.toml packages/core/
COPY packages/harness/pyproject.toml packages/harness/
COPY packages/harness_claude/pyproject.toml packages/harness_claude/
COPY packages/store/pyproject.toml packages/store/
COPY packages/channels/pyproject.toml packages/channels/
COPY packages/channel_telegram/pyproject.toml packages/channel_telegram/
COPY packages/media/pyproject.toml packages/media/
RUN uv sync --frozen --no-dev --no-install-workspace

COPY apps apps
COPY packages packages
COPY config config
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 claudia \
    && mkdir -p /data /srv/claudia/work/sister \
    && chown -R claudia:claudia /data /srv/claudia/work
USER claudia

EXPOSE 8000
CMD ["claudia", "serve"]
