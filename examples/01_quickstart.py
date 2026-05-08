"""Quickstart: create a task, list it, read it, then complete it manually.

Run the server first:

    kanban serve   # or: python -m kanban_core.server

Then in another terminal:

    pip install httpx
    python examples/01_quickstart.py
"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=5.0) as c:
        # 1. Create a task. Idempotency key makes re-runs safe.
        created = c.post(
            "/tasks",
            json={
                "title": "Summarize today's PRs",
                "body": "Read the diff and produce a 3-bullet summary.",
                "type": "summarize",
                "assignee": "claude",
                "priority": 5,
                "payload": {"repo": "lincleejun/kanban", "since": "1d"},
                "idempotency_key": "demo-quickstart-1",
            },
        ).raise_for_status().json()
        task_id = created["id"]
        print(f"created  {task_id}  status={created['status']}")

        # 2. List ready tasks.
        listed = c.get("/tasks", params={"status": "ready"}).raise_for_status().json()
        print(f"ready    {[t['id'] for t in listed['items']]}")

        # 3. Read full record + worker context (consistent snapshot).
        ctx = c.get(f"/tasks/{task_id}/context").raise_for_status().json()
        print(f"context  title={ctx['task']['title']!r} payload={ctx['task']['payload']}")

        # 4. Claim it as a worker would.
        claim = c.post(
            "/tasks/claim",
            json={"worker_id": "demo-worker", "lease_seconds": 30},
        ).raise_for_status().json()
        print(f"claimed  {claim['id']} -> running, lease={claim.get('lease_expires_at')}")

        # 5. Complete it with a result.
        c.post(
            f"/tasks/{task_id}/complete",
            json={
                "worker_id": "demo-worker",
                "result": {"bullets": ["a", "b", "c"]},
                "summary": "done",
            },
        ).raise_for_status()
        final = c.get(f"/tasks/{task_id}").raise_for_status().json()
        print(f"final    status={final['status']}")


if __name__ == "__main__":
    main()
