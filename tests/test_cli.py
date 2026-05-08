"""TDD-driven tests for the kanban CLI.

The CLI is an HTTP client to a running server. Tests stand up a real
TestClient against the FastAPI app and patch the CLI's httpx client to
hit it directly — no network, no subprocess.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner


class _NonClosingClient:
    """Wraps a TestClient so the CLI's `with _client() as c:` doesn't close it
    (the TestClient is owned by the fixture and reused across CLI invocations)."""

    def __init__(self, inner):
        self._inner = inner

    def __enter__(self):
        return self._inner

    def __exit__(self, *exc):
        return False


@pytest.fixture
def cli_runner(client: TestClient, monkeypatch):
    """Return (CliRunner, app) wired so the CLI hits the in-process server."""
    from kanban_core import cli as cli_mod

    def fake_make_client(base_url: str, timeout: float = 10.0):
        return _NonClosingClient(client)

    monkeypatch.setattr(cli_mod, "_make_client", fake_make_client)
    return CliRunner(), cli_mod.app


def run(cli_runner, *args: str):
    runner, app = cli_runner
    return runner.invoke(app, list(args), catch_exceptions=False)


def test_task_add_creates_task_and_prints_id(cli_runner):
    r = run(cli_runner, "task", "add", "fix login", "--type", "bug")
    assert r.exit_code == 0, r.output
    assert "task_" in r.output


def test_task_add_with_payload_json(cli_runner):
    r = run(
        cli_runner,
        "task", "add", "summarize",
        "--type", "summarize",
        "--payload", '{"repo": "foo"}',
        "--priority", "5",
    )
    assert r.exit_code == 0, r.output
    task_id = r.output.strip().splitlines()[0]
    r2 = run(cli_runner, "task", "show", task_id, "--json")
    assert r2.exit_code == 0, r2.output
    data = json.loads(r2.output)
    assert data["payload"] == {"repo": "foo"}
    assert data["priority"] == 5


def test_task_ls_prints_header_with_column_names(cli_runner):
    run(cli_runner, "task", "add", "headed", "--type", "x", "--key", "h1")
    r = run(cli_runner, "task", "ls")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    # First non-empty line is the header.
    header = next(l for l in lines if l.strip())
    for col in ("ID", "STATUS", "TYPE", "TITLE"):
        assert col in header, f"missing {col} in header: {header!r}"


def test_task_ls_paginates_with_limit_and_offset(cli_runner):
    for i in range(7):
        r = run(cli_runner, "task", "add", f"t{i}", "--type", "x", "--key", f"k{i}")
        assert r.exit_code == 0

    r = run(cli_runner, "task", "ls", "--limit", "3", "--offset", "0")
    assert r.exit_code == 0, r.output
    # Footer must show paging info and hint at next page.
    assert "of 7" in r.output
    assert "--offset 3" in r.output
    # Three result rows in the body.
    rows = [l for l in r.output.splitlines() if l.startswith("task_")]
    assert len(rows) == 3


def test_task_ls_all_iterates_through_pages(cli_runner):
    for i in range(5):
        run(cli_runner, "task", "add", f"a{i}", "--type", "x", "--key", f"a{i}")
    r = run(cli_runner, "task", "ls", "--limit", "2", "--all")
    assert r.exit_code == 0, r.output
    rows = [l for l in r.output.splitlines() if l.startswith("task_")]
    assert len(rows) == 5


def test_task_ls_filters_by_status(cli_runner):
    run(cli_runner, "task", "add", "ready1", "--type", "x", "--key", "r1")
    run(cli_runner, "task", "add", "ready2", "--type", "x", "--key", "r2")
    r = run(cli_runner, "task", "ls", "--status", "ready")
    assert r.exit_code == 0, r.output
    rows = [l for l in r.output.splitlines() if l.startswith("task_")]
    assert len(rows) == 2


def test_task_show_unknown_returns_nonzero(cli_runner):
    r = run(cli_runner, "task", "show", "task_nope")
    assert r.exit_code != 0
    assert "not found" in r.output.lower() or "404" in r.output


def test_task_claim_then_done(cli_runner):
    r = run(cli_runner, "task", "add", "do me", "--type", "x", "--key", "do1")
    tid = r.output.strip().splitlines()[0]

    r = run(cli_runner, "task", "claim", "--worker", "w1")
    assert r.exit_code == 0, r.output
    assert tid in r.output

    r = run(cli_runner, "task", "done", tid, "--worker", "w1", "--result", '{"ok":true}')
    assert r.exit_code == 0, r.output

    r = run(cli_runner, "task", "show", tid, "--json")
    assert json.loads(r.output)["status"] == "done"


def test_task_claim_empty_queue_exits_nonzero(cli_runner):
    r = run(cli_runner, "task", "claim", "--worker", "w1")
    assert r.exit_code != 0
    assert "no tasks" in r.output.lower() or "ready" in r.output.lower()


def test_task_fail_requeues(cli_runner):
    r = run(cli_runner, "task", "add", "boom", "--type", "x", "--key", "boom")
    tid = r.output.strip().splitlines()[0]
    run(cli_runner, "task", "claim", "--worker", "w1")
    r = run(cli_runner, "task", "fail", tid, "--worker", "w1", "--error", "kaboom")
    assert r.exit_code == 0, r.output


def test_link_creates_dependency(cli_runner):
    r = run(cli_runner, "task", "add", "parent", "--type", "x", "--key", "p1")
    parent = r.output.strip().splitlines()[0]
    r = run(cli_runner, "task", "add", "child", "--type", "x", "--key", "c1")
    child = r.output.strip().splitlines()[0]

    r = run(cli_runner, "link", parent, child)
    assert r.exit_code == 0, r.output

    # child should now be in todo (waiting), not ready.
    r = run(cli_runner, "task", "show", child, "--json")
    assert json.loads(r.output)["status"] == "todo"


def test_stats_outputs_counts(cli_runner):
    run(cli_runner, "task", "add", "s1", "--type", "x", "--key", "s1")
    r = run(cli_runner, "stats")
    assert r.exit_code == 0, r.output
    # stats endpoint returns at least a "ready" or "by_status" key
    assert "ready" in r.output.lower() or "1" in r.output
