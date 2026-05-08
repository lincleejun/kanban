# Examples

Runnable demos for Kanban Core. Start the server first:

```bash
pip install -e ".[dev]" httpx
kanban serve                # serves on http://127.0.0.1:8000
```

Then in another terminal:

| File | What it shows |
|------|---------------|
| [`01_quickstart.py`](01_quickstart.py) | Create → list → context → claim → complete. The 5-line tour. |
| [`02_worker.py`](02_worker.py) | A polling worker loop with heartbeat, retry on failure, graceful exit. |
| [`03_dependencies.py`](03_dependencies.py) | Build a 4-task DAG via `/links` and watch `ready` propagate as parents complete. |

Each example is self-contained — run them in order, or in isolation. They
all use stable `idempotency_key`s, so re-running won't create duplicates.

To reset state between runs:

```bash
rm kanban.db
```
