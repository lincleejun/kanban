"""Repository protocol — the contract a storage backend must satisfy.

The default backend is `SqliteTaskRepository`. Other backends (in-memory,
Postgres, etc.) only need to satisfy this Protocol.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from .models import Task, TaskComment, TaskEvent, TaskRun


class TaskRepository(Protocol):
    def create_task(
        self,
        title: str,
        body: str = "",
        *,
        assignee: str | None = None,
        created_by: str = "user",
        type: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        status: str | None = None,
        parents: list[str] | None = None,
        workspace_kind: str = "scratch",
        workspace_path: str | None = None,
        tenant: str | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        max_runtime_seconds: int | None = None,
    ) -> Task: ...

    def claim(
        self,
        worker_id: str,
        types: list[str] | None,
        lease_seconds: int,
        *,
        assignee: str | None = None,
        tenant: str | None = None,
    ) -> Task | None: ...

    def heartbeat(
        self, task_id: str, worker_id: str, lease_seconds: int, note: str | None = None
    ) -> Task | None: ...

    def complete(
        self,
        task_id: str,
        worker_id: str,
        result: Any = None,
        *,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task | None: ...

    def fail(self, task_id: str, worker_id: str, error: str, requeue: bool) -> Task | None: ...

    def block(self, task_id: str, actor: str, reason: str) -> Task | None: ...

    def unblock(self, task_id: str, actor: str = "user") -> Task | None: ...

    def archive(self, task_id: str, actor: str = "user") -> Task | None: ...

    def set_status(self, task_id: str, status: str, actor: str = "user") -> Task | None: ...

    def add_comment(self, task_id: str, author: str, body: str) -> TaskComment | None: ...

    def link_tasks(self, parent_id: str, child_id: str, actor: str = "user") -> bool: ...

    def unlink_tasks(self, parent_id: str, child_id: str, actor: str = "user") -> bool: ...

    def dispatch_once(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        assignee: str | None = None,
    ) -> dict[str, Any]: ...

    def build_worker_context(self, task_id: str) -> dict[str, Any] | None: ...

    def get(self, task_id: str) -> Task | None: ...

    def list(
        self,
        status: str | None,
        type: str | None,
        limit: int,
        offset: int,
        *,
        assignee: str | None = None,
        tenant: str | None = None,
    ) -> tuple[list[Task], int]: ...

    def list_comments(self, task_id: str) -> list[TaskComment]: ...

    def list_events(self, task_id: str, limit: int = 100) -> list[TaskEvent]: ...

    def list_runs(self, task_id: str, limit: int = 50) -> list[TaskRun]: ...

    def stats(self) -> dict[str, int]: ...

    def release_stale_claims(self, now: datetime | None = None) -> int: ...

    def recompute_ready(self) -> int: ...
