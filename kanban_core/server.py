"""ASGI entrypoint. `python -m kanban_core.server` (or `kanban serve`) runs uvicorn."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import router
from .config import settings
from .sqlite_repo import SqliteTaskRepository
from .sweeper import run_sweeper


def create_app(repo=None) -> FastAPI:
    repo = repo or SqliteTaskRepository(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.repo = repo
        task = asyncio.create_task(run_sweeper(repo, settings.sweep_interval_seconds))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="Kanban Core",
        description="Headless kanban task graph for agent workers.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "kanban_core.server:app",
        host=settings.http_host,
        port=settings.http_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
