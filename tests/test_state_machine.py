import pytest


def test_create_then_claim_then_complete(repo):
    task = repo.create_task("Render page", type="render", payload={"x": 1}, priority=5)
    assert task.status == "ready"
    assert task.consecutive_failures == 0

    claimed = repo.claim("codex", None, lease_seconds=60)
    assert claimed is not None
    assert claimed.id == task.id
    assert claimed.status == "running"
    assert claimed.claim_lock == "codex"
    assert claimed.current_run_id is not None

    done = repo.complete(task.id, "codex", {"ok": True}, summary="rendered")
    assert done is not None
    assert done.status == "done"
    assert done.result == {"ok": True}
    assert done.completed_at is not None


def test_complete_with_wrong_worker_rejected(repo):
    task = repo.create_task("Render page")
    repo.claim("codex", None, lease_seconds=60)

    assert repo.complete(task.id, "claude", {"ok": True}) is None

    current = repo.get(task.id)
    assert current.status == "running"
    assert current.claim_lock == "codex"


def test_fail_with_requeue_returns_to_ready(repo):
    task = repo.create_task("Render page", max_attempts=3)
    repo.claim("codex", None, 60)

    out = repo.fail(task.id, "codex", "boom", requeue=True)

    assert out.status == "ready"
    assert out.claim_lock is None
    assert out.consecutive_failures == 1
    assert out.last_failure_error == "boom"


def test_fail_does_not_overwrite_result(repo):
    task = repo.create_task("T", max_attempts=2)
    repo.claim("w", None, 60)
    failed = repo.fail(task.id, "w", "boom", requeue=True)
    assert failed.result is None
    assert failed.last_failure_error == "boom"


def test_fail_exhausts_attempts_blocks_task(repo):
    task = repo.create_task("Render page", max_attempts=2)
    for _ in range(2):
        claimed = repo.claim("codex", None, 60)
        assert claimed is not None
        repo.fail(claimed.id, "codex", "boom", requeue=True)

    final = repo.get(task.id)
    assert final.status == "blocked"


def test_priority_order(repo):
    a = repo.create_task("low", priority=1)
    b = repo.create_task("high", priority=10)
    c = repo.create_task("mid", priority=5)

    assert repo.claim("w", None, 60).id == b.id
    assert repo.claim("w", None, 60).id == c.id
    assert repo.claim("w", None, 60).id == a.id


def test_claim_filters_by_type(repo):
    a = repo.create_task("alpha task", type="alpha")
    b = repo.create_task("beta task", type="beta")

    assert repo.claim("w", ["beta"], 60).id == b.id
    assert repo.claim("w", ["beta"], 60) is None
    assert repo.claim("w", ["alpha"], 60).id == a.id


def test_claim_empty_returns_none(repo):
    assert repo.claim("w", None, 60) is None


def test_dependencies_hold_child_until_parent_done(repo):
    parent = repo.create_task("Design API")
    child = repo.create_task("Implement worker", parents=[parent.id])

    assert repo.get(child.id).status == "todo"
    assert repo.claim("w", None, 60).id == parent.id
    assert repo.claim("w", None, 60) is None

    repo.complete(parent.id, "w", {"ok": True})

    ready_child = repo.get(child.id)
    assert ready_child.status == "ready"
    assert repo.claim("w", None, 60).id == child.id


def test_block_running_closes_active_run(repo):
    task = repo.create_task("T")
    repo.claim("w", None, 60)
    blocked = repo.block(task.id, "user", "halt")
    assert blocked.status == "blocked"
    assert blocked.current_run_id is None
    assert blocked.claim_lock is None
    runs = repo.list_runs(task.id)
    assert len(runs) == 1
    assert runs[0].status == "blocked"
    assert runs[0].ended_at is not None


def test_unblock_promotes_when_parents_done(repo):
    parent = repo.create_task("P")
    child = repo.create_task("C", parents=[parent.id])
    repo.block(child.id, "user", "wait")
    out = repo.unblock(child.id)
    # parent still pending -> child returns to todo
    assert out.status == "todo"

    repo.claim("w", None, 60)
    repo.complete(parent.id, "w", {})
    # now child is auto-promoted via complete()
    assert repo.get(child.id).status == "ready"


def test_archive_only_from_done(repo):
    task = repo.create_task("T")
    assert repo.archive(task.id) is None  # not done yet
    repo.claim("w", None, 60)
    repo.complete(task.id, "w", {})
    assert repo.archive(task.id).status == "archived"
    # idempotent: archiving again returns None (no longer 'done')
    assert repo.archive(task.id) is None


def test_set_status_rejects_running_target(repo):
    task = repo.create_task("T")
    with pytest.raises(ValueError):
        repo.set_status(task.id, "running")


def test_set_status_rejects_done_target(repo):
    task = repo.create_task("T")
    with pytest.raises(ValueError):
        repo.set_status(task.id, "done")


def test_set_status_cannot_leave_running(repo):
    task = repo.create_task("T")
    repo.claim("w", None, 60)
    with pytest.raises(ValueError):
        repo.set_status(task.id, "ready")


def test_set_status_blocked_to_ready(repo):
    task = repo.create_task("T")
    repo.block(task.id, "user", "halt")
    out = repo.set_status(task.id, "ready")
    assert out.status == "ready"


def test_link_cycle_rejected(repo):
    a = repo.create_task("A")
    b = repo.create_task("B")
    assert repo.link_tasks(a.id, b.id) is True
    assert repo.link_tasks(b.id, a.id) is False  # would create cycle


def test_link_self_rejected(repo):
    a = repo.create_task("A")
    assert repo.link_tasks(a.id, a.id) is False


def test_idempotent_create(repo):
    a = repo.create_task("A", idempotency_key="key1")
    b = repo.create_task("B", idempotency_key="key1")
    assert a.id == b.id


def test_comments_events_runs_and_context(repo):
    task = repo.create_task("Implement Codex role", assignee="codex")
    comment = repo.add_comment(task.id, "user", "Use codex for code edits")
    claimed = repo.claim("codex", None, 60, assignee="codex")
    repo.heartbeat(task.id, "codex", 60, note="still working")
    repo.complete(task.id, "codex", {"ok": True}, summary="done")

    context = repo.build_worker_context(task.id)

    assert comment is not None
    assert claimed.id == task.id
    assert context["task"]["id"] == task.id
    assert context["comments"][0]["body"] == "Use codex for code edits"
    assert context["runs"][0]["status"] == "done"
    assert [e["event_type"] for e in context["recent_events"]] == [
        "created",
        "commented",
        "claimed",
        "heartbeat",
        "completed",
    ]


def test_stats(repo):
    repo.create_task("one")
    repo.create_task("two")
    claimed = repo.claim("w", None, 60)
    repo.complete(claimed.id, "w", {})

    stats = repo.stats()

    assert stats["ready"] == 1
    assert stats["done"] == 1
