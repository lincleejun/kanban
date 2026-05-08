"""Build a small task DAG and watch it unblock.

Pipeline:

    fetch ──▶ analyze ──▶ report
                   ▲
    fetch_logs ────┘

`analyze` becomes `ready` only after both `fetch` and `fetch_logs` are
`done`. `report` waits on `analyze`. We simulate completion to show the
state propagation.

Run the server first:

    python -m app.main
    python examples/03_dependencies.py
"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"


def create(c: httpx.Client, title: str, key: str) -> str:
    return c.post(
        "/tasks",
        json={"title": title, "type": "demo", "idempotency_key": key},
    ).raise_for_status().json()["id"]


def status(c: httpx.Client, tid: str) -> str:
    return c.get(f"/tasks/{tid}").raise_for_status().json()["status"]


def claim_and_complete(c: httpx.Client, worker: str) -> str:
    r = c.post("/tasks/claim", json={"worker_id": worker, "lease_seconds": 30})
    if r.status_code == 204:
        return ""
    task = r.raise_for_status().json()
    c.post(
        f"/tasks/{task['id']}/complete",
        json={"worker_id": worker, "result": {"ok": True}, "summary": "auto"},
    ).raise_for_status()
    return task["id"]


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=5.0) as c:
        fetch = create(c, "fetch", "demo-dag-fetch")
        logs = create(c, "fetch_logs", "demo-dag-logs")
        analyze = create(c, "analyze", "demo-dag-analyze")
        report = create(c, "report", "demo-dag-report")

        # Wire dependencies: analyze depends on (fetch, logs); report on analyze.
        for parent, child in [(fetch, analyze), (logs, analyze), (analyze, report)]:
            c.post("/links", json={"parent_id": parent, "child_id": child}).raise_for_status()

        def snapshot(label: str) -> None:
            print(f"-- {label}")
            for tid, name in [(fetch, "fetch"), (logs, "logs"),
                              (analyze, "analyze"), (report, "report")]:
                print(f"   {name:8s} {tid[:8]}  {status(c, tid)}")

        snapshot("initial (only roots are ready)")

        while True:
            done_id = claim_and_complete(c, "demo-runner")
            if not done_id:
                break
            print(f"   completed {done_id[:8]}")

        snapshot("final")


if __name__ == "__main__":
    main()
