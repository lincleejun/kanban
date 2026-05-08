# Kanban Core

A small, headless task queue for multi-agent workers — durable tasks,
dependency-aware scheduling, atomic claims with leases, and per-run history.
SQLite + FastAPI, no UI, no external services.

Designed to run agents like Codex CLI, Claude Code, or any custom worker
against a shared backlog without coordinating through the file system.

```
   ┌────────┐  POST /tasks         ┌──────────────┐
   │  user  ├─────────────────────▶│              │
   └────────┘                      │              │
                                   │  Kanban Core │
   ┌────────┐  POST /tasks/claim   │  (FastAPI +  │
   │ worker ├─────────────────────▶│   SQLite)    │
   │ codex  │  POST .../heartbeat  │              │
   │ claude │  POST .../complete   │              │
   └────────┘                      └──────────────┘
```

## Why

Most agent setups either:

- spawn a CLI per task and lose state when it crashes, or
- glue together a workflow engine that's heavier than the task graph itself.

This project sits between the two: a single-file SQLite store with a
durable lease/claim protocol so a worker can crash and the task still
gets retried — without bringing in Redis, Postgres, or Celery.

## Install

Requires Python 3.11+.

```bash
pip install -e ".[dev,cli]"
kanban serve                # serves on http://127.0.0.1:8000
```

Open http://127.0.0.1:8000/docs for the interactive OpenAPI UI.

## CLI

A typer-based CLI ships under the `[cli]` extra and talks to a running server:

```bash
kanban serve                                  # start the server
kanban task add "fix login" --type bug        # create a task; prints id
kanban task ls --status ready --limit 20      # list with pagination
kanban task ls --all                          # iterate all pages
kanban task show <id>                         # human-readable; --json for raw
kanban task claim --worker me                 # atomic claim
kanban task done <id> --worker me --result '{"ok":true}'
kanban task fail <id> --worker me --error "boom"
kanban link <parent_id> <child_id>            # add dependency
kanban stats                                  # counts by status
```

Pagination flags on `kanban task ls`:

- `--limit N` (default 50, max 500), `--offset N` — direct passthrough to the API
- `--all` — auto-iterate every page (use with care on large datasets)
- footer shows `Showing 1-50 of 137 (use --offset 50 for next page)`

The CLI honours `KANBAN_URL` for non-default servers (default `http://127.0.0.1:8000`).

## Concepts

| Concept    | Meaning                                                            |
|------------|--------------------------------------------------------------------|
| **Task**   | The durable unit of work — title, body, type, payload, priority.   |
| **Status** | `triage` → `todo` → `ready` → `running` → `done` (`/blocked`/`archived`). |
| **Link**   | `parent → child` dependency. Child enters `ready` only when all parents are `done`. |
| **Claim**  | A worker atomically transitions one `ready` task to `running` and gets a lease. |
| **Lease**  | A timed lock. Workers must `heartbeat` to extend, or the sweeper requeues the task. |
| **Run**    | One execution attempt. A task may have many runs across retries.   |
| **Event**  | Append-only audit log of everything that happened to a task.       |

## API

| Method   | Path                                  | Purpose                                   |
|----------|---------------------------------------|-------------------------------------------|
| `POST`   | `/tasks`                              | Create a task (idempotent via key)        |
| `GET`    | `/tasks` `?status=&type=&assignee=`   | List tasks (paginated)                    |
| `GET`    | `/tasks/{id}`                         | Read one task                             |
| `GET`    | `/tasks/{id}/context`                 | Worker handoff packet (consistent snapshot) |
| `GET`    | `/tasks/{id}/runs` \| `/comments` \| `/events` | Per-task history                  |
| `POST`   | `/tasks/claim`                        | Atomically claim one ready task           |
| `POST`   | `/tasks/{id}/heartbeat`               | Extend the lease                          |
| `POST`   | `/tasks/{id}/complete`                | Mark done with a result                   |
| `POST`   | `/tasks/{id}/fail`                    | Retry or block                            |
| `POST`   | `/tasks/{id}/block`                   | Manual block with reason                  |
| `POST`   | `/tasks/{id}/unblock`                 | Return a blocked task to the queue        |
| `POST`   | `/tasks/{id}/archive`                 | Archive a `done` task                     |
| `POST`   | `/tasks/{id}/status`                  | Restricted manual status change           |
| `POST`   | `/tasks/{id}/comments`                | Add a collaboration note                  |
| `POST`   | `/links`                              | Add `parent → child` dependency           |
| `DELETE` | `/links/{parent_id}/{child_id}`       | Remove dependency                         |
| `POST`   | `/dispatch`                           | Sweep + claim + return context, in one call |
| `GET`    | `/stats` \| `/healthz`                | Counts and liveness                       |

`set_status` is intentionally **restricted**: it cannot move tasks into
`running` or `done` (those go through `claim` and `complete`), nor leave
`running` (use `complete` / `fail` / `block`). This keeps the lease
invariant intact.

## Worker example

```python
import requests, socket, time

base = "http://127.0.0.1:8000"
me = f"codex-{socket.gethostname()}"

while True:
    r = requests.post(
        f"{base}/tasks/claim",
        json={"worker_id": me, "assignee": "codex", "lease_seconds": 60},
    )
    if r.status_code == 204:
        time.sleep(2)
        continue

    task = r.json()
    context = requests.get(f"{base}/tasks/{task['id']}/context").json()

    try:
        result = run_codex(context)         # your worker
        requests.post(
            f"{base}/tasks/{task['id']}/complete",
            json={"worker_id": me, "result": result, "summary": "ok"},
        )
    except Exception as exc:
        requests.post(
            f"{base}/tasks/{task['id']}/fail",
            json={"worker_id": me, "error": str(exc), "requeue": True},
        )
```

If the worker crashes, the lease expires and the sweeper returns the task
to `ready` (or `blocked` once `max_attempts` is reached). No coordination
or external lock service required.

## Configuration

All knobs are environment variables:

| Variable                  | Default          | Meaning                              |
|---------------------------|------------------|--------------------------------------|
| `KANBAN_DB_PATH`          | `./kanban.db`    | SQLite file path                     |
| `DEFAULT_LEASE_SECONDS`   | `60`             | Lease applied when a worker omits it |
| `SWEEP_INTERVAL_SECONDS`  | `10`             | How often the sweeper scans for expired leases |
| `HTTP_HOST` / `HTTP_PORT` | `127.0.0.1:8000` | Bind address                         |
| `LOG_LEVEL`               | `INFO`           | Standard Python log levels           |

## Design

See [docs/design.md](docs/design.md) for the full state machine, scheduling
rules, lease recovery, and how to attach Codex / Claude Code workers.

## Testing

```bash
pip install -e ".[dev,cli]"
pytest
```

Tests cover the state machine, claim concurrency (10 workers × 50 tasks),
lease expiry, and the HTTP surface.

## Dependencies

Three runtime dependencies, all mainstream:

- `fastapi` — HTTP layer.
- `uvicorn` — ASGI server.
- `pydantic` — request validation.

SQLite is in the Python standard library; no daemon to install.

## License

MIT — see [LICENSE](LICENSE).
