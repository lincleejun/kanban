"""A minimal long-running worker.

Polls for ready tasks, claims one, sends heartbeats while "working",
then completes (or fails for retry). Survives restarts because the
lease will expire and the sweeper requeues the task.

Run the server first:

    kanban serve   # or: python -m kanban_core.server

Seed a few tasks (e.g. via examples/01_quickstart.py or curl), then:

    python examples/02_worker.py
"""

from __future__ import annotations

import os
import random
import socket
import time

import httpx

BASE = os.environ.get("KANBAN_URL", "http://127.0.0.1:8000")
WORKER_ID = f"demo-{socket.gethostname()}-{os.getpid()}"
LEASE = 30  # seconds
POLL = 2    # seconds between empty polls


def do_work(task: dict) -> dict:
    """Replace this with your actual worker — call Codex, Claude, anything."""
    print(f"  working on {task['id']!r} ({task.get('type')})")
    time.sleep(random.uniform(0.5, 2.0))
    if random.random() < 0.15:
        raise RuntimeError("simulated transient failure")
    return {"ok": True, "echo": task.get("payload")}


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10.0) as c:
        print(f"worker {WORKER_ID} polling {BASE}")
        while True:
            r = c.post(
                "/tasks/claim",
                json={"worker_id": WORKER_ID, "lease_seconds": LEASE},
            )
            if r.status_code == 204:
                time.sleep(POLL)
                continue
            r.raise_for_status()
            task = r.json()
            tid = task["id"]
            print(f"claimed {tid}")

            # Refresh lease at half-life while doing the work. In a real
            # worker this would run in a background thread.
            c.post(
                f"/tasks/{tid}/heartbeat",
                json={"worker_id": WORKER_ID, "lease_seconds": LEASE, "note": "alive"},
            ).raise_for_status()

            try:
                result = do_work(task)
                c.post(
                    f"/tasks/{tid}/complete",
                    json={"worker_id": WORKER_ID, "result": result, "summary": "ok"},
                ).raise_for_status()
                print(f"  done    {tid}")
            except Exception as exc:
                c.post(
                    f"/tasks/{tid}/fail",
                    json={"worker_id": WORKER_ID, "error": str(exc), "requeue": True},
                ).raise_for_status()
                print(f"  failed  {tid}: {exc} (requeue)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
