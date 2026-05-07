"""HTTP API surface.

All routes are thin adapters over `TaskRepository`. Errors map to:
  404 — resource missing
  409 — lease/state mismatch (worker doesn't hold the lease, etc.)
  422 — invalid transition (set_status on running, etc.)
"""

from fastapi import APIRouter, HTTPException, Query, Request, Response

from . import schemas
from .config import settings
from .repo import TaskRepository

router = APIRouter()


def _repo(request: Request) -> TaskRepository:
    return request.app.state.repo


def _serialize(value) -> dict:
    return value.to_dict()


@router.get("/")
def root() -> dict:
    return {
        "name": "Kanban Core",
        "description": "Headless kanban task graph for agent workers.",
        "version": 1,
    }


@router.get("/healthz")
def healthz(request: Request) -> dict:
    # Cheap query confirms the repo is reachable.
    _repo(request).stats()
    return {"ok": True}


# --- task lifecycle -----------------------------------------------------------


@router.post("/tasks", status_code=201)
def create_task(req: schemas.CreateTaskReq, request: Request) -> dict:
    title = req.title or (f"{req.type} task" if req.type else "Untitled task")
    try:
        task = _repo(request).create_task(
            title=title,
            body=req.body,
            assignee=req.assignee,
            created_by=req.created_by,
            type=req.type,
            payload=req.payload,
            priority=req.priority,
            status=req.status,
            parents=req.parents,
            workspace_kind=req.workspace_kind,
            workspace_path=req.workspace_path,
            tenant=req.tenant,
            idempotency_key=req.idempotency_key,
            max_attempts=req.max_attempts,
            max_runtime_seconds=req.max_runtime_seconds,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _serialize(task)


@router.post("/tasks/claim")
def claim(req: schemas.ClaimReq, request: Request, response: Response):
    lease = req.lease_seconds or settings.default_lease_seconds
    task = _repo(request).claim(
        req.worker_id,
        req.types,
        lease,
        assignee=req.assignee,
        tenant=req.tenant,
    )
    if task is None:
        response.status_code = 204
        return None
    return _serialize(task)


@router.post("/tasks/{task_id}/heartbeat")
def heartbeat(task_id: str, req: schemas.HeartbeatReq, request: Request) -> dict:
    lease = req.lease_seconds or settings.default_lease_seconds
    task = _repo(request).heartbeat(task_id, req.worker_id, lease, note=req.note)
    if task is None:
        raise HTTPException(409, "task not held by this worker or not running")
    return {
        "task_id": task.id,
        "claim_expires_at": task.claim_expires_at.isoformat() if task.claim_expires_at else None,
        "last_heartbeat_at": task.last_heartbeat_at.isoformat() if task.last_heartbeat_at else None,
    }


@router.post("/tasks/{task_id}/complete")
def complete(task_id: str, req: schemas.CompleteReq, request: Request) -> dict:
    task = _repo(request).complete(
        task_id,
        req.worker_id,
        req.result,
        summary=req.summary,
        metadata=req.metadata,
    )
    if task is None:
        raise HTTPException(409, "task not held by this worker or not running")
    return _serialize(task)


@router.post("/tasks/{task_id}/fail")
def fail(task_id: str, req: schemas.FailReq, request: Request) -> dict:
    task = _repo(request).fail(task_id, req.worker_id, req.error, req.requeue)
    if task is None:
        raise HTTPException(409, "task not held by this worker or not running")
    return _serialize(task)


@router.post("/tasks/{task_id}/block")
def block(task_id: str, req: schemas.BlockReq, request: Request) -> dict:
    task = _repo(request).block(task_id, req.actor, req.reason)
    if task is None:
        raise HTTPException(404, "task not found or already terminal")
    return _serialize(task)


@router.post("/tasks/{task_id}/unblock")
def unblock(task_id: str, req: schemas.UnblockReq, request: Request) -> dict:
    task = _repo(request).unblock(task_id, req.actor)
    if task is None:
        raise HTTPException(409, "task not in 'blocked' state")
    return _serialize(task)


@router.post("/tasks/{task_id}/archive")
def archive(task_id: str, req: schemas.ArchiveReq, request: Request) -> dict:
    task = _repo(request).archive(task_id, req.actor)
    if task is None:
        raise HTTPException(409, "task not in 'done' state")
    return _serialize(task)


@router.post("/tasks/{task_id}/status")
def set_status(task_id: str, req: schemas.StatusReq, request: Request) -> dict:
    """Manual status override. Restricted: cannot target running/done, cannot
    leave running/archived. Use claim/complete/fail/block/unblock/archive
    for the normal lifecycle transitions."""
    try:
        task = _repo(request).set_status(task_id, req.status, req.actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if task is None:
        raise HTTPException(404, "task not found")
    return _serialize(task)


@router.post("/tasks/{task_id}/comments", status_code=201)
def add_comment(task_id: str, req: schemas.CommentReq, request: Request) -> dict:
    comment = _repo(request).add_comment(task_id, req.author, req.body)
    if comment is None:
        raise HTTPException(404, "task not found")
    return _serialize(comment)


# --- dependency graph ---------------------------------------------------------


@router.post("/links", status_code=201)
def link_tasks(req: schemas.LinkReq, request: Request) -> dict:
    ok = _repo(request).link_tasks(req.parent_id, req.child_id, req.actor)
    if not ok:
        raise HTTPException(409, "parent or child missing, self-link, or would create a cycle")
    return {"ok": True}


@router.delete("/links/{parent_id}/{child_id}")
def unlink_tasks(
    parent_id: str,
    child_id: str,
    request: Request,
    actor: str = Query("user", max_length=schemas.ID_MAX),
) -> dict:
    ok = _repo(request).unlink_tasks(parent_id, child_id, actor)
    if not ok:
        raise HTTPException(404, "link not found")
    return {"ok": True}


# --- read API -----------------------------------------------------------------


@router.post("/dispatch")
def dispatch_once(req: schemas.DispatchReq, request: Request) -> dict:
    lease = req.lease_seconds or settings.default_lease_seconds
    return _repo(request).dispatch_once(
        req.worker_id,
        lease_seconds=lease,
        assignee=req.assignee,
    )


@router.get("/tasks/{task_id}/context")
def worker_context(task_id: str, request: Request) -> dict:
    context = _repo(request).build_worker_context(task_id)
    if context is None:
        raise HTTPException(404, "task not found")
    return context


@router.get("/tasks/{task_id}/runs")
def list_runs(
    task_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    return {"items": [r.to_dict() for r in _repo(request).list_runs(task_id, limit)]}


@router.get("/tasks/{task_id}/comments")
def list_comments(task_id: str, request: Request) -> dict:
    return {"items": [c.to_dict() for c in _repo(request).list_comments(task_id)]}


@router.get("/tasks/{task_id}/events")
def list_events(
    task_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    return {"items": [e.to_dict() for e in _repo(request).list_events(task_id, limit)]}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict:
    task = _repo(request).get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return _serialize(task)


@router.get("/tasks")
def list_tasks(
    request: Request,
    status: str | None = None,
    type: str | None = None,
    assignee: str | None = None,
    tenant: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    items, total = _repo(request).list(
        status,
        type,
        limit,
        offset,
        assignee=assignee,
        tenant=tenant,
    )
    return {"items": [_serialize(task) for task in items], "total": total}


@router.get("/stats")
def stats(request: Request) -> dict:
    return _repo(request).stats()
