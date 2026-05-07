from typing import Any
from pydantic import BaseModel, Field


class EnqueueReq(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    max_attempts: int = 3


class ClaimReq(BaseModel):
    worker_id: str
    types: list[str] | None = None
    lease_seconds: int | None = None


class HeartbeatReq(BaseModel):
    worker_id: str
    lease_seconds: int | None = None


class CompleteReq(BaseModel):
    worker_id: str
    result: Any = None


class FailReq(BaseModel):
    worker_id: str
    error: str
    requeue: bool = True


class TaskOut(BaseModel):
    id: int
    type: str
    payload: dict[str, Any]
    status: str
    priority: int
    attempts: int
    max_attempts: int
    worker_id: str | None
    lease_expires_at: str | None
    result: Any
    created_at: str
    updated_at: str
