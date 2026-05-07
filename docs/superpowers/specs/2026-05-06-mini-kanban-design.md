# Mini Kanban Task Queue — Design Spec

Date: 2026-05-06
Status: Approved for planning

## 1. Goal & Scope

Build a minimal task management / queue system inspired by kanboard, primarily serving an **agent / worker pool** that polls and claims tasks via HTTP, with a secondary **read-only human view** for inspecting status.

**In scope**
- Queue-style task lifecycle with safe concurrent claim
- Lease + heartbeat to recover from worker crashes
- Pluggable storage layer (SQLite as default)
- REST API for enqueue / claim / heartbeat / complete / fail / list / stats
- Read-only HTML dashboard grouping tasks by status
- Tests for concurrency, lease expiry, state-machine validity

**Out of scope (YAGNI)**
- Auth / multi-tenant
- Cron / scheduled tasks
- Drag-and-drop UI editing
- Distributed coordination beyond single SQLite file

## 2. Stack

- Python 3.11+
- FastAPI for HTTP
- `sqlite3` (stdlib) for storage; WAL mode enabled
- `pydantic` v2 for request/response schemas
- `pytest` for tests

## 3. State Machine

```
pending ──claim──▶ claimed ──start──▶ running ──┬─▶ success
   ▲                  │                          │
   │                  └── lease expired ─┐       └─▶ failed
   │                                     │              │
   └────── sweeper requeues ─────────────┘              │
   ▲                                                    │
   └──── attempts < max_attempts (auto-requeue) ────────┘
```

Allowed transitions (any other transition returns 409):

| From | To | Trigger |
|---|---|---|
| pending | claimed | `POST /tasks/claim` |
| claimed | running | implicit on first heartbeat or explicit start (we treat claim as already running for simplicity — see §6) |
| claimed/running | success | `POST /tasks/{id}/complete` |
| claimed/running | failed | `POST /tasks/{id}/fail` with `requeue=false` or attempts exhausted |
| claimed/running | pending | `POST /tasks/{id}/fail` with `requeue=true` and attempts < max, **or** sweeper finds expired lease |

Decision: collapse `claimed` and `running` into a single state `running`. The "claim" API moves `pending → running` and assigns a lease. This removes a redundant state without losing safety.

Final states list: `pending`, `running`, `success`, `failed`.

## 4. Data Model

```sql
CREATE TABLE tasks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  type             TEXT    NOT NULL,
  payload          TEXT    NOT NULL,                  -- JSON string
  status           TEXT    NOT NULL CHECK (status IN ('pending','running','success','failed')),
  priority         INTEGER NOT NULL DEFAULT 0,        -- higher = sooner
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 3,
  worker_id        TEXT,
  lease_expires_at TEXT,                              -- ISO-8601 UTC, NULL when not running
  result           TEXT,                              -- JSON string for success result or error message
  created_at       TEXT    NOT NULL,
  updated_at       TEXT    NOT NULL
);
CREATE INDEX idx_tasks_claim ON tasks(status, type, priority DESC, id);
CREATE INDEX idx_tasks_lease ON tasks(status, lease_expires_at);
```

`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;` set on every connection.

## 5. Storage Abstraction

`app/repo.py` defines a `TaskRepository` Protocol:

```python
class TaskRepository(Protocol):
    def enqueue(self, type: str, payload: dict, priority: int = 0,
                max_attempts: int = 3) -> Task: ...
    def claim(self, worker_id: str, types: list[str] | None,
              lease_seconds: int) -> Task | None: ...
    def heartbeat(self, task_id: int, worker_id: str,
                  lease_seconds: int) -> bool: ...
    def complete(self, task_id: int, worker_id: str, result: dict) -> Task: ...
    def fail(self, task_id: int, worker_id: str, error: str,
             requeue: bool) -> Task: ...
    def get(self, task_id: int) -> Task | None: ...
    def list(self, status: str | None, type: str | None,
             limit: int, offset: int) -> list[Task]: ...
    def stats(self) -> dict[str, int]: ...
    def sweep_expired(self, now: datetime) -> int: ...
```

`SqliteTaskRepository` in `app/sqlite_repo.py` is the default implementation. Other backends (Postgres, Redis) can plug in by implementing the Protocol.

### Atomic claim (the key mechanism)

```sql
BEGIN IMMEDIATE;
UPDATE tasks
SET status = 'running',
    worker_id = :worker_id,
    attempts = attempts + 1,
    lease_expires_at = :lease_expires_at,
    updated_at = :now
WHERE id = (
  SELECT id FROM tasks
  WHERE status = 'pending'
    AND (:types IS NULL OR type IN (...))
  ORDER BY priority DESC, id ASC
  LIMIT 1
)
RETURNING *;
COMMIT;
```

`BEGIN IMMEDIATE` plus the subselect guarantees no two workers claim the same row.

### Heartbeat / complete / fail

All three include `worker_id = :worker_id AND status = 'running'` in the WHERE clause. Mismatch returns `False` / 409 — protects against a worker that lost its lease still trying to write.

### Fail logic

- If `requeue=True` and `attempts < max_attempts`: status → `pending`, clear `worker_id`/`lease_expires_at`, store last error in `result`.
- Otherwise: status → `failed`, store error in `result`.

## 6. HTTP API

Base path `/`.

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/tasks` | `{type, payload, priority?, max_attempts?}` | 201 Task |
| POST | `/tasks/claim` | `{worker_id, types?: [str], lease_seconds?: int=60}` | 200 Task or 204 |
| POST | `/tasks/{id}/heartbeat` | `{worker_id, lease_seconds?: int=60}` | 200 `{lease_expires_at}` or 409 |
| POST | `/tasks/{id}/complete` | `{worker_id, result: dict}` | 200 Task or 409 |
| POST | `/tasks/{id}/fail` | `{worker_id, error: str, requeue?: bool=true}` | 200 Task or 409 |
| GET | `/tasks/{id}` | — | 200 Task or 404 |
| GET | `/tasks` | query: `status, type, limit=50, offset=0` | 200 `{items, total}` |
| GET | `/stats` | — | 200 `{pending, running, success, failed}` |
| GET | `/` | — | 200 HTML dashboard |

`Task` JSON shape mirrors the table, with `payload` and `result` as parsed JSON.

## 7. Sweeper

A FastAPI `lifespan` context spawns one `asyncio.create_task` running:

```
while not shutdown:
    n = repo.sweep_expired(datetime.utcnow())
    await asyncio.sleep(10)
```

`sweep_expired` does, in a single transaction, for every row with `status='running' AND lease_expires_at < now`:

- if `attempts < max_attempts`: status → `pending`, clear `worker_id`/`lease_expires_at`, append timeout note to `result`
- else: status → `failed`, `result = "lease expired, attempts exhausted"`

Sweep interval (10s) and lease default (60s) are configured via env vars `SWEEP_INTERVAL_SECONDS`, `DEFAULT_LEASE_SECONDS`.

## 8. Read-only Web View

`GET /` returns a single static HTML page (`app/web/index.html`) that:

- Polls `/stats` and `/tasks?status=...&limit=20` every few seconds via `fetch`
- Renders 4 columns (pending / running / success / failed), each showing id, type, priority, worker_id, attempts, age
- No edit controls

Plain HTML + a small `<script>` block. No build step, no framework.

## 9. Project Layout

```
kanban/
  app/
    __init__.py
    main.py          # FastAPI app, lifespan starts sweeper, mounts routes & static
    api.py           # Route handlers
    schemas.py       # Pydantic request/response models
    models.py        # Task dataclass
    repo.py          # TaskRepository Protocol
    sqlite_repo.py   # SQLite implementation
    sweeper.py       # background coroutine
    config.py        # env-var settings
    web/index.html   # read-only dashboard
  tests/
    conftest.py
    test_claim_concurrency.py
    test_lease_expiry.py
    test_state_machine.py
    test_api.py
  pyproject.toml
  README.md
```

## 10. Testing Strategy

- **Concurrency**: spawn N threads each calling `repo.claim(...)`, assert each task claimed by exactly one worker.
- **Lease expiry**: enqueue, claim with 1s lease, sleep, run `sweep_expired`, assert task back to `pending` and `attempts==1`. Repeat until `attempts==max_attempts`, assert ends in `failed`.
- **State machine guards**: try `complete` from `pending`, `claim` a `success` task, `heartbeat` with wrong worker — all must return 409 / False.
- **API**: FastAPI `TestClient` covering happy paths and the above guard cases.
- **Stats**: enqueue mixed states, assert `/stats` counts.

## 11. Configuration

Env vars (with defaults):

- `KANBAN_DB_PATH=./kanban.db`
- `DEFAULT_LEASE_SECONDS=60`
- `SWEEP_INTERVAL_SECONDS=10`
- `HTTP_HOST=127.0.0.1`
- `HTTP_PORT=8000`

## 12. Non-Goals / Future Work

- Auth, RBAC
- Multiple priority queues with weighting
- Webhooks on completion
- Postgres backend (interface ready, impl deferred)
- Metrics / Prometheus exporter
