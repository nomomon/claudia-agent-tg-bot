from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from claudia_bot.config import Settings
from claudia_bot.container import build_container


def create_app(settings: Settings | None = None) -> FastAPI:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = settings or Settings()  # type: ignore[call-arg]  # values come from env / .env
    container = build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await container.bot.session.close()
            container.store.close()

    app = FastAPI(title="claudia", lifespan=lifespan)
    app.state.container = container
    app.include_router(container.channel.router(settings.webhook_path))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
