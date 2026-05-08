from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


TASK_STATUSES = (
    "triage",
    "todo",
    "ready",
    "running",
    "blocked",
    "done",
    "archived",
)
ACTIVE_STATUSES = ("triage", "todo", "ready", "running", "blocked")
TERMINAL_STATUSES = ("done", "archived")


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


@dataclass
class Task:
    id: str
    title: str
    body: str
    status: str
    priority: int
    assignee: str | None
    created_by: str
    type: str | None
    payload: dict[str, Any]
    workspace_kind: str
    workspace_path: str | None
    tenant: str | None
    idempotency_key: str | None
    claim_lock: str | None
    claim_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    current_run_id: str | None
    max_attempts: int
    consecutive_failures: int
    last_failure_error: str | None
    max_runtime_seconds: int | None
    result: Any
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass
class TaskRun:
    id: str
    task_id: str
    worker_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    heartbeat_at: datetime | None
    error: str | None
    summary: str | None
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass
class TaskComment:
    id: str
    task_id: str
    author: str
    body: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass
class TaskEvent:
    id: str
    task_id: str
    event_type: str
    actor: str | None
    data: dict[str, Any]
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))
