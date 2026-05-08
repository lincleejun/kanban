"""Pydantic request schemas for the HTTP API.

String fields that map to indexed DB columns or actor identities have a
length cap so a misbehaving client can't bloat storage."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# Reasonable upper bounds for free-form identity / routing strings.
ID_MAX = 128
SHORT_TEXT_MAX = 256
LONG_TEXT_MAX = 8192


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTaskReq(_Strict):
    title: str | None = Field(default=None, max_length=SHORT_TEXT_MAX)
    body: str = Field(default="", max_length=LONG_TEXT_MAX)
    assignee: str | None = Field(default=None, max_length=ID_MAX)
    created_by: str = Field(default="user", max_length=ID_MAX)
    type: str | None = Field(default=None, max_length=ID_MAX)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-1_000_000, le=1_000_000)
    status: str | None = Field(default=None, max_length=32)
    parents: list[str] = Field(default_factory=list, max_length=128)
    workspace_kind: str = Field(default="scratch", max_length=ID_MAX)
    workspace_path: str | None = Field(default=None, max_length=SHORT_TEXT_MAX)
    tenant: str | None = Field(default=None, max_length=ID_MAX)
    idempotency_key: str | None = Field(default=None, max_length=ID_MAX)
    max_attempts: int = Field(default=3, ge=1, le=1000)
    max_runtime_seconds: int | None = Field(default=None, ge=1, le=30 * 24 * 3600)


class ClaimReq(_Strict):
    worker_id: str = Field(min_length=1, max_length=ID_MAX)
    types: list[str] | None = Field(default=None, max_length=64)
    assignee: str | None = Field(default=None, max_length=ID_MAX)
    tenant: str | None = Field(default=None, max_length=ID_MAX)
    lease_seconds: int | None = Field(default=None, ge=1, le=24 * 3600)


class HeartbeatReq(_Strict):
    worker_id: str = Field(min_length=1, max_length=ID_MAX)
    lease_seconds: int | None = Field(default=None, ge=1, le=24 * 3600)
    note: str | None = Field(default=None, max_length=SHORT_TEXT_MAX)


class CompleteReq(_Strict):
    worker_id: str = Field(min_length=1, max_length=ID_MAX)
    result: Any = None
    summary: str | None = Field(default=None, max_length=SHORT_TEXT_MAX)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FailReq(_Strict):
    worker_id: str = Field(min_length=1, max_length=ID_MAX)
    error: str = Field(max_length=LONG_TEXT_MAX)
    requeue: bool = True


class BlockReq(_Strict):
    actor: str = Field(default="user", max_length=ID_MAX)
    reason: str = Field(max_length=SHORT_TEXT_MAX)


class UnblockReq(_Strict):
    actor: str = Field(default="user", max_length=ID_MAX)


class ArchiveReq(_Strict):
    actor: str = Field(default="user", max_length=ID_MAX)


class StatusReq(_Strict):
    actor: str = Field(default="user", max_length=ID_MAX)
    status: str = Field(max_length=32)


class CommentReq(_Strict):
    author: str = Field(min_length=1, max_length=ID_MAX)
    body: str = Field(min_length=1, max_length=LONG_TEXT_MAX)


class LinkReq(_Strict):
    parent_id: str = Field(min_length=1, max_length=ID_MAX)
    child_id: str = Field(min_length=1, max_length=ID_MAX)
    actor: str = Field(default="user", max_length=ID_MAX)


class DispatchReq(_Strict):
    worker_id: str = Field(min_length=1, max_length=ID_MAX)
    assignee: str | None = Field(default=None, max_length=ID_MAX)
    lease_seconds: int | None = Field(default=None, ge=1, le=24 * 3600)
