"""Kanban CLI — HTTP client for the Kanban Core API.

    pip install kanban-core
    kanban serve
    kanban task add "fix login" --type bug
    kanban task ls --status ready --limit 20
"""

from __future__ import annotations

import json as jsonlib
import os
from typing import Any

import httpx
import typer

app = typer.Typer(no_args_is_help=True, help="Kanban Core CLI.")
task_app = typer.Typer(no_args_is_help=True, help="Manage tasks.")
app.add_typer(task_app, name="task")


def _make_client(base_url: str, timeout: float = 10.0) -> httpx.Client:
    """Build the HTTP client. Patched in tests to use ASGI transport."""
    return httpx.Client(base_url=base_url, timeout=timeout)


def _base_url() -> str:
    return os.environ.get("KANBAN_URL", "http://127.0.0.1:8000")


def _client() -> httpx.Client:
    return _make_client(_base_url())


def _check(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        typer.echo(f"error: HTTP {r.status_code}: {detail}", err=True)
        raise typer.Exit(code=1)
    if r.status_code == 204:
        return None
    return r.json()


# --- serve ---------------------------------------------------------------

@app.command()
def serve() -> None:
    """Start the Kanban HTTP server (uvicorn)."""
    from .server import main as server_main
    server_main()


# --- task add ------------------------------------------------------------

@task_app.command("add")
def task_add(
    title: str = typer.Argument(..., help="Task title."),
    type: str | None = typer.Option(None, "--type", help="Task type."),
    body: str = typer.Option("", "--body", help="Task body / description."),
    assignee: str | None = typer.Option(None, "--assignee"),
    priority: int = typer.Option(0, "--priority"),
    payload: str = typer.Option("{}", "--payload", help="JSON payload."),
    parents: list[str] = typer.Option([], "--parent", help="Parent task id (repeatable)."),
    key: str | None = typer.Option(None, "--key", help="Idempotency key."),
) -> None:
    """Create a task. Prints the new task id."""
    try:
        payload_obj = jsonlib.loads(payload)
    except jsonlib.JSONDecodeError as exc:
        typer.echo(f"error: --payload is not valid JSON: {exc}", err=True)
        raise typer.Exit(2)

    body_req: dict[str, Any] = {
        "title": title,
        "body": body,
        "priority": priority,
        "payload": payload_obj,
        "parents": parents,
    }
    if type is not None:
        body_req["type"] = type
    if assignee is not None:
        body_req["assignee"] = assignee
    if key is not None:
        body_req["idempotency_key"] = key

    with _client() as c:
        data = _check(c.post("/tasks", json=body_req))
    typer.echo(data["id"])


# --- task ls -------------------------------------------------------------

@task_app.command("ls")
def task_ls(
    status: str | None = typer.Option(None, "--status"),
    type: str | None = typer.Option(None, "--type"),
    assignee: str | None = typer.Option(None, "--assignee"),
    tenant: str | None = typer.Option(None, "--tenant"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    all_: bool = typer.Option(False, "--all", help="Iterate every page."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List tasks with pagination."""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if type:
        params["type"] = type
    if assignee:
        params["assignee"] = assignee
    if tenant:
        params["tenant"] = tenant

    items: list[dict] = []
    total = 0
    with _client() as c:
        while True:
            data = _check(c.get("/tasks", params=params))
            items.extend(data["items"])
            total = data["total"]
            if not all_:
                break
            params["offset"] += params["limit"]
            if params["offset"] >= total or not data["items"]:
                break

    if json_:
        typer.echo(jsonlib.dumps({"items": items, "total": total}, indent=2))
        return

    for t in items:
        typer.echo(
            f"{t['id']}  {t['status']:<8}  {t.get('type') or '-':<12}  {t.get('title') or ''}"
        )

    if all_:
        typer.echo(f"-- {len(items)} of {total}")
    else:
        shown_lo = offset + 1 if items else 0
        shown_hi = offset + len(items)
        next_off = offset + limit
        suffix = f" (use --offset {next_off} for next page)" if next_off < total else ""
        typer.echo(f"-- showing {shown_lo}-{shown_hi} of {total}{suffix}")


# --- task show -----------------------------------------------------------

@task_app.command("show")
def task_show(
    task_id: str = typer.Argument(...),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    """Show a single task."""
    with _client() as c:
        data = _check(c.get(f"/tasks/{task_id}"))
    if json_:
        typer.echo(jsonlib.dumps(data, indent=2))
    else:
        for k in ("id", "status", "type", "title", "assignee", "priority", "attempts"):
            typer.echo(f"{k:<10} {data.get(k)}")


# --- task claim ----------------------------------------------------------

@task_app.command("claim")
def task_claim(
    worker: str = typer.Option(..., "--worker"),
    type: list[str] = typer.Option([], "--type"),
    assignee: str | None = typer.Option(None, "--assignee"),
    lease: int | None = typer.Option(None, "--lease"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    """Atomically claim one ready task."""
    body: dict[str, Any] = {"worker_id": worker}
    if type:
        body["types"] = type
    if assignee:
        body["assignee"] = assignee
    if lease:
        body["lease_seconds"] = lease

    with _client() as c:
        r = c.post("/tasks/claim", json=body)
    if r.status_code == 204:
        typer.echo("no ready tasks", err=True)
        raise typer.Exit(1)
    data = _check(r)
    if json_:
        typer.echo(jsonlib.dumps(data, indent=2))
    else:
        typer.echo(data["id"])


# --- task done -----------------------------------------------------------

@task_app.command("done")
def task_done(
    task_id: str = typer.Argument(...),
    worker: str = typer.Option(..., "--worker"),
    result: str = typer.Option("null", "--result", help="JSON result."),
    summary: str | None = typer.Option(None, "--summary"),
) -> None:
    """Complete a running task."""
    try:
        result_obj = jsonlib.loads(result)
    except jsonlib.JSONDecodeError as exc:
        typer.echo(f"error: --result is not valid JSON: {exc}", err=True)
        raise typer.Exit(2)
    body = {"worker_id": worker, "result": result_obj}
    if summary:
        body["summary"] = summary
    with _client() as c:
        _check(c.post(f"/tasks/{task_id}/complete", json=body))
    typer.echo(f"done {task_id}")


# --- task fail -----------------------------------------------------------

@task_app.command("fail")
def task_fail(
    task_id: str = typer.Argument(...),
    worker: str = typer.Option(..., "--worker"),
    error: str = typer.Option(..., "--error"),
    no_requeue: bool = typer.Option(False, "--no-requeue"),
) -> None:
    """Fail a running task (requeue by default)."""
    body = {"worker_id": worker, "error": error, "requeue": not no_requeue}
    with _client() as c:
        _check(c.post(f"/tasks/{task_id}/fail", json=body))
    typer.echo(f"failed {task_id}")


# --- task block / unblock / archive --------------------------------------

@task_app.command("block")
def task_block(task_id: str, reason: str = typer.Option(..., "--reason"),
               actor: str = typer.Option("user", "--actor")) -> None:
    """Manually block a task."""
    with _client() as c:
        _check(c.post(f"/tasks/{task_id}/block", json={"reason": reason, "actor": actor}))
    typer.echo(f"blocked {task_id}")


@task_app.command("unblock")
def task_unblock(task_id: str, actor: str = typer.Option("user", "--actor")) -> None:
    """Return a blocked task to the queue."""
    with _client() as c:
        _check(c.post(f"/tasks/{task_id}/unblock", json={"actor": actor}))
    typer.echo(f"unblocked {task_id}")


@task_app.command("archive")
def task_archive(task_id: str, actor: str = typer.Option("user", "--actor")) -> None:
    """Archive a done task."""
    with _client() as c:
        _check(c.post(f"/tasks/{task_id}/archive", json={"actor": actor}))
    typer.echo(f"archived {task_id}")


# --- link ----------------------------------------------------------------

@app.command()
def link(parent: str, child: str, actor: str = typer.Option("user", "--actor")) -> None:
    """Add a parent → child dependency."""
    with _client() as c:
        _check(c.post("/links", json={"parent_id": parent, "child_id": child, "actor": actor}))
    typer.echo(f"linked {parent} -> {child}")


# --- stats ---------------------------------------------------------------

@app.command()
def stats() -> None:
    """Show task counts by status."""
    with _client() as c:
        data = _check(c.get("/stats"))
    typer.echo(jsonlib.dumps(data, indent=2))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
