def test_create_and_claim_via_api(client):
    response = client.post(
        "/tasks",
        json={"title": "Render", "type": "render", "payload": {"x": 1}, "priority": 5},
    )
    assert response.status_code == 201
    task_id = response.json()["id"]

    response = client.post("/tasks/claim", json={"worker_id": "codex"})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == task_id
    assert body["status"] == "running"
    assert body["claim_lock"] == "codex"


def test_claim_empty_returns_204(client):
    response = client.post("/tasks/claim", json={"worker_id": "codex"})
    assert response.status_code == 204


def test_complete_flow(client):
    client.post("/tasks", json={"title": "Do work", "type": "t", "payload": {}})
    claimed = client.post("/tasks/claim", json={"worker_id": "codex"}).json()

    response = client.post(
        f"/tasks/{claimed['id']}/complete",
        json={"worker_id": "codex", "result": {"ok": 1}, "summary": "done"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["result"] == {"ok": 1}


def test_complete_wrong_worker_409(client):
    client.post("/tasks", json={"title": "Do work"})
    claimed = client.post("/tasks/claim", json={"worker_id": "codex"}).json()

    response = client.post(
        f"/tasks/{claimed['id']}/complete",
        json={"worker_id": "claude", "result": {}},
    )

    assert response.status_code == 409


def test_fail_requeue(client):
    client.post("/tasks", json={"title": "Do work", "max_attempts": 3})
    claimed = client.post("/tasks/claim", json={"worker_id": "codex"}).json()

    response = client.post(
        f"/tasks/{claimed['id']}/fail",
        json={"worker_id": "codex", "error": "oops", "requeue": True},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_heartbeat_extends_lease(client):
    client.post("/tasks", json={"title": "Do work"})
    claimed = client.post(
        "/tasks/claim", json={"worker_id": "codex", "lease_seconds": 5}
    ).json()

    response = client.post(
        f"/tasks/{claimed['id']}/heartbeat",
        json={"worker_id": "codex", "lease_seconds": 60},
    )

    assert response.status_code == 200
    assert "claim_expires_at" in response.json()


def test_get_list_stats_and_context(client):
    parent = client.post("/tasks", json={"title": "Parent"}).json()
    child = client.post(
        "/tasks", json={"title": "Child", "parents": [parent["id"]]}
    ).json()
    client.post(
        f"/tasks/{child['id']}/comments",
        json={"author": "user", "body": "wait for parent"},
    )

    listing = client.get("/tasks").json()
    assert listing["total"] == 2

    only_ready = client.get("/tasks?status=ready").json()
    assert only_ready["total"] == 1

    stats = client.get("/stats").json()
    assert stats["ready"] == 1
    assert stats["todo"] == 1

    context = client.get(f"/tasks/{child['id']}/context")
    assert context.status_code == 200
    assert context.json()["parents"][0]["id"] == parent["id"]
    assert context.json()["comments"][0]["body"] == "wait for parent"


def test_link_block_dispatch_and_root(client):
    first = client.post("/tasks", json={"title": "First"}).json()
    second = client.post("/tasks", json={"title": "Second"}).json()

    link = client.post("/links", json={"parent_id": first["id"], "child_id": second["id"]})
    assert link.status_code == 201
    assert client.get(f"/tasks/{second['id']}").json()["status"] == "todo"

    block = client.post(
        f"/tasks/{first['id']}/block",
        json={"actor": "user", "reason": "needs input"},
    )
    assert block.status_code == 200
    assert block.json()["status"] == "blocked"

    dispatch = client.post("/dispatch", json={"worker_id": "codex"}).json()
    assert dispatch["claimed"] is False

    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["name"] == "Kanban Core"


def test_unblock_returns_to_ready_when_no_parents(client):
    task = client.post("/tasks", json={"title": "T"}).json()
    client.post(f"/tasks/{task['id']}/block", json={"reason": "wait"})
    response = client.post(f"/tasks/{task['id']}/unblock", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_unblock_returns_to_todo_when_parents_pending(client):
    parent = client.post("/tasks", json={"title": "P"}).json()
    child = client.post(
        "/tasks", json={"title": "C", "parents": [parent["id"]]}
    ).json()
    client.post(f"/tasks/{child['id']}/block", json={"reason": "wait"})
    response = client.post(f"/tasks/{child['id']}/unblock", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "todo"


def test_archive_only_from_done(client):
    task = client.post("/tasks", json={"title": "T"}).json()
    archive_pending = client.post(f"/tasks/{task['id']}/archive", json={})
    assert archive_pending.status_code == 409

    client.post("/tasks/claim", json={"worker_id": "w"})
    client.post(f"/tasks/{task['id']}/complete", json={"worker_id": "w"})
    archive_done = client.post(f"/tasks/{task['id']}/archive", json={})
    assert archive_done.status_code == 200
    assert archive_done.json()["status"] == "archived"


def test_set_status_rejects_running_and_done(client):
    task = client.post("/tasks", json={"title": "T"}).json()
    bad = client.post(
        f"/tasks/{task['id']}/status", json={"status": "running"}
    )
    assert bad.status_code == 422


def test_unlink_uses_path_params(client):
    p = client.post("/tasks", json={"title": "P"}).json()
    c = client.post("/tasks", json={"title": "C", "parents": [p["id"]]}).json()
    response = client.delete(f"/links/{p['id']}/{c['id']}")
    assert response.status_code == 200


def test_idempotent_create(client):
    a = client.post("/tasks", json={"title": "A", "idempotency_key": "k1"}).json()
    b = client.post("/tasks", json={"title": "A2", "idempotency_key": "k1"}).json()
    assert a["id"] == b["id"]


def test_link_cycle_rejected(client):
    a = client.post("/tasks", json={"title": "A"}).json()
    b = client.post("/tasks", json={"title": "B"}).json()
    ok = client.post("/links", json={"parent_id": a["id"], "child_id": b["id"]})
    assert ok.status_code == 201
    cycle = client.post("/links", json={"parent_id": b["id"], "child_id": a["id"]})
    assert cycle.status_code == 409


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_runs_and_events_endpoints(client):
    task = client.post("/tasks", json={"title": "T"}).json()
    client.post("/tasks/claim", json={"worker_id": "w"})
    client.post(f"/tasks/{task['id']}/complete", json={"worker_id": "w"})

    runs = client.get(f"/tasks/{task['id']}/runs").json()
    assert len(runs["items"]) == 1
    assert runs["items"][0]["status"] == "done"

    events = client.get(f"/tasks/{task['id']}/events").json()
    types = [e["event_type"] for e in events["items"]]
    assert "created" in types and "completed" in types
