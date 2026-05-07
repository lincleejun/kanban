# Mini Kanban

A minimal task queue for agent workers, with a read-only kanban view for humans.
Inspired by [kanboard](https://github.com/kanboard/kanboard) but optimized for
worker pools that poll-and-claim rather than humans dragging cards.

## Features

- Queue-style state machine: `pending → running → success/failed`
- Atomic, race-free `claim` (one task → one worker)
- Lease + heartbeat: crashed workers' tasks auto-requeue
- Per-task `type`, `priority`, `max_attempts`
- Pluggable storage layer (default: SQLite)
- Read-only HTML dashboard at `/`

## Install & run

```bash
pip install -e .[dev]
python -m app.main          # or: uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000/ for the dashboard.

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/tasks` | Enqueue `{type, payload, priority?, max_attempts?}` |
| `POST` | `/tasks/claim` | Worker claims one task `{worker_id, types?, lease_seconds?}` (returns 204 if empty) |
| `POST` | `/tasks/{id}/heartbeat` | Extend lease `{worker_id, lease_seconds?}` |
| `POST` | `/tasks/{id}/complete` | Mark success `{worker_id, result}` |
| `POST` | `/tasks/{id}/fail` | Mark failed `{worker_id, error, requeue?}` |
| `GET` | `/tasks/{id}` | Get one task |
| `GET` | `/tasks?status=&type=&limit=&offset=` | List |
| `GET` | `/stats` | `{pending, running, success, failed}` |
| `GET` | `/` | HTML dashboard |

## Worker example

```python
import requests, time, socket

base = "http://127.0.0.1:8000"
me = f"worker-{socket.gethostname()}"

while True:
    r = requests.post(f"{base}/tasks/claim", json={"worker_id": me, "lease_seconds": 60})
    if r.status_code == 204:
        time.sleep(2); continue
    task = r.json()
    try:
        result = do_work(task["type"], task["payload"])
        requests.post(f"{base}/tasks/{task['id']}/complete",
                      json={"worker_id": me, "result": result})
    except Exception as e:
        requests.post(f"{base}/tasks/{task['id']}/fail",
                      json={"worker_id": me, "error": str(e), "requeue": True})
```

## Configuration (env vars)

- `KANBAN_DB_PATH` (default `./kanban.db`)
- `DEFAULT_LEASE_SECONDS` (default `60`)
- `SWEEP_INTERVAL_SECONDS` (default `10`)
- `HTTP_HOST` (default `127.0.0.1`)
- `HTTP_PORT` (default `8000`)

## Storage abstraction

`app/repo.py` defines `TaskRepository` as a Protocol. To plug in another
backend (Postgres, Redis...) implement that interface and pass it to
`create_app(repo=...)`.

## Testing

```bash
pytest
```

Covers: state machine, lease expiry & sweeper, concurrent claim, HTTP API.
