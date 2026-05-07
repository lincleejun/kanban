import asyncio
from datetime import datetime, timezone

from .repo import TaskRepository


async def run_sweeper(repo: TaskRepository, interval_seconds: int) -> None:
    while True:
        try:
            await asyncio.to_thread(repo.sweep_expired, datetime.now(timezone.utc))
        except Exception:
            # Don't kill the sweeper loop on transient errors.
            pass
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
