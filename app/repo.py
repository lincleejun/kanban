from datetime import datetime
from typing import Any, Protocol

from .models import Task


class TaskRepository(Protocol):
    def enqueue(
        self,
        type: str,
        payload: dict[str, Any],
        priority: int = 0,
        max_attempts: int = 3,
    ) -> Task: ...

    def claim(
        self,
        worker_id: str,
        types: list[str] | None,
        lease_seconds: int,
    ) -> Task | None: ...

    def heartbeat(
        self, task_id: int, worker_id: str, lease_seconds: int
    ) -> Task | None: ...

    def complete(
        self, task_id: int, worker_id: str, result: Any
    ) -> Task | None: ...

    def fail(
        self, task_id: int, worker_id: str, error: str, requeue: bool
    ) -> Task | None: ...

    def get(self, task_id: int) -> Task | None: ...

    def list(
        self,
        status: str | None,
        type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Task], int]: ...

    def stats(self) -> dict[str, int]: ...

    def sweep_expired(self, now: datetime) -> int: ...
