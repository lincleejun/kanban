from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pathlib import Path

from . import schemas
from .config import settings
from .repo import TaskRepository


router = APIRouter()

_INDEX_HTML = (Path(__file__).parent / "web" / "index.html").read_text()


def _repo(request: Request) -> TaskRepository:
    return request.app.state.repo


def _serialize(task) -> dict:
    return task.to_dict()


@router.post("/tasks", status_code=201)
def enqueue(req: schemas.EnqueueReq, request: Request) -> dict:
    t = _repo(request).enqueue(
        req.type, req.payload, req.priority, req.max_attempts
    )
    return _serialize(t)


@router.post("/tasks/claim")
def claim(req: schemas.ClaimReq, request: Request, response: Response):
    lease = req.lease_seconds or settings.default_lease_seconds
    t = _repo(request).claim(req.worker_id, req.types, lease)
    if t is None:
        response.status_code = 204
        return None
    return _serialize(t)


@router.post("/tasks/{task_id}/heartbeat")
def heartbeat(task_id: int, req: schemas.HeartbeatReq, request: Request) -> dict:
    lease = req.lease_seconds or settings.default_lease_seconds
    t = _repo(request).heartbeat(task_id, req.worker_id, lease)
    if t is None:
        raise HTTPException(409, "task not held by this worker or not running")
    return {"lease_expires_at": t.lease_expires_at.isoformat()}


@router.post("/tasks/{task_id}/complete")
def complete(task_id: int, req: schemas.CompleteReq, request: Request) -> dict:
    t = _repo(request).complete(task_id, req.worker_id, req.result)
    if t is None:
        raise HTTPException(409, "task not held by this worker or not running")
    return _serialize(t)


@router.post("/tasks/{task_id}/fail")
def fail(task_id: int, req: schemas.FailReq, request: Request) -> dict:
    t = _repo(request).fail(task_id, req.worker_id, req.error, req.requeue)
    if t is None:
        raise HTTPException(409, "task not held by this worker or not running")
    return _serialize(t)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, request: Request) -> dict:
    t = _repo(request).get(task_id)
    if t is None:
        raise HTTPException(404, "not found")
    return _serialize(t)


@router.get("/tasks")
def list_tasks(
    request: Request,
    status: str | None = None,
    type: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    items, total = _repo(request).list(status, type, limit, offset)
    return {"items": [_serialize(t) for t in items], "total": total}


@router.get("/stats")
def stats(request: Request) -> dict:
    return _repo(request).stats()


@router.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML
