"""Background sweeper: periodically releases tasks whose worker leases
have expired, returning them to `ready` (or `blocked` if attempts exhausted)."""

import asyncio
import logging
from datetime import datetime, timezone

from .repo import TaskRepository

log = logging.getLogger(__name__)


async def run_sweeper(repo: TaskRepository, interval_seconds: int) -> None:
    while True:
        try:
            released = await asyncio.to_thread(
                repo.release_stale_claims, datetime.now(timezone.utc)
            )
            if released:
                log.info("sweeper released %d stale claim(s)", released)
        except asyncio.CancelledError:
            return
        except Exception:
            # Log and keep the loop alive; transient SQLite contention or a
            # programming error must not silently take down the sweeper.
            log.exception("sweeper iteration failed")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
