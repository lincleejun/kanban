def test_enqueue_and_claim_via_api(client):
    r = client.post("/tasks", json={"type": "render", "payload": {"x": 1}, "priority": 5})
    assert r.status_code == 201
    tid = r.json()["id"]

    r = client.post("/tasks/claim", json={"worker_id": "w1"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == tid
    assert body["status"] == "running"
    assert body["worker_id"] == "w1"


def test_claim_empty_returns_204(client):
    r = client.post("/tasks/claim", json={"worker_id": "w1"})
    assert r.status_code == 204


def test_complete_flow(client):
    client.post("/tasks", json={"type": "t", "payload": {}})
    claim = client.post("/tasks/claim", json={"worker_id": "w1"}).json()
    tid = claim["id"]

    r = client.post(f"/tasks/{tid}/complete", json={"worker_id": "w1", "result": {"ok": 1}})
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    assert r.json()["result"] == {"ok": 1}


def test_complete_wrong_worker_409(client):
    client.post("/tasks", json={"type": "t", "payload": {}})
    claim = client.post("/tasks/claim", json={"worker_id": "w1"}).json()
    tid = claim["id"]
    r = client.post(f"/tasks/{tid}/complete", json={"worker_id": "w2", "result": {}})
    assert r.status_code == 409


def test_fail_requeue(client):
    client.post("/tasks", json={"type": "t", "payload": {}, "max_attempts": 3})
    claim = client.post("/tasks/claim", json={"worker_id": "w1"}).json()
    tid = claim["id"]
    r = client.post(f"/tasks/{tid}/fail", json={"worker_id": "w1", "error": "oops", "requeue": True})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


def test_heartbeat_extends_lease(client):
    client.post("/tasks", json={"type": "t", "payload": {}})
    claim = client.post("/tasks/claim", json={"worker_id": "w1", "lease_seconds": 5}).json()
    tid = claim["id"]
    r = client.post(f"/tasks/{tid}/heartbeat", json={"worker_id": "w1", "lease_seconds": 60})
    assert r.status_code == 200
    assert "lease_expires_at" in r.json()


def test_get_and_list_and_stats(client):
    for i in range(3):
        client.post("/tasks", json={"type": "t", "payload": {"i": i}})
    listing = client.get("/tasks").json()
    assert listing["total"] == 3
    assert len(listing["items"]) == 3

    only_pending = client.get("/tasks?status=pending").json()
    assert only_pending["total"] == 3

    stats = client.get("/stats").json()
    assert stats["pending"] == 3

    one = listing["items"][0]
    g = client.get(f"/tasks/{one['id']}")
    assert g.status_code == 200


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Mini Kanban" in r.text
