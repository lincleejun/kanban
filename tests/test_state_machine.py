def test_enqueue_then_claim_then_complete(repo):
    t = repo.enqueue("render", {"x": 1}, priority=5)
    assert t.status == "pending"
    assert t.attempts == 0

    claimed = repo.claim("w1", None, lease_seconds=60)
    assert claimed is not None
    assert claimed.id == t.id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.worker_id == "w1"

    done = repo.complete(t.id, "w1", {"ok": True})
    assert done is not None
    assert done.status == "success"
    assert done.result == {"ok": True}


def test_complete_with_wrong_worker_rejected(repo):
    t = repo.enqueue("render", {})
    repo.claim("w1", None, lease_seconds=60)
    assert repo.complete(t.id, "w2", {"ok": True}) is None
    # task still running, owned by w1
    g = repo.get(t.id)
    assert g.status == "running"
    assert g.worker_id == "w1"


def test_complete_on_pending_rejected(repo):
    t = repo.enqueue("render", {})
    assert repo.complete(t.id, "w1", {}) is None


def test_fail_with_requeue_returns_to_pending(repo):
    t = repo.enqueue("render", {}, max_attempts=3)
    repo.claim("w1", None, 60)
    out = repo.fail(t.id, "w1", "boom", requeue=True)
    assert out.status == "pending"
    assert out.worker_id is None
    assert out.attempts == 1


def test_fail_exhausts_attempts(repo):
    t = repo.enqueue("render", {}, max_attempts=2)
    for _ in range(2):
        c = repo.claim("w1", None, 60)
        assert c is not None
        repo.fail(c.id, "w1", "boom", requeue=True)
    final = repo.get(t.id)
    # second fail with attempts==max moves to failed
    assert final.status == "failed"


def test_priority_order(repo):
    a = repo.enqueue("t", {}, priority=1)
    b = repo.enqueue("t", {}, priority=10)
    c = repo.enqueue("t", {}, priority=5)
    first = repo.claim("w", None, 60)
    assert first.id == b.id
    second = repo.claim("w", None, 60)
    assert second.id == c.id
    third = repo.claim("w", None, 60)
    assert third.id == a.id


def test_claim_filters_by_type(repo):
    a = repo.enqueue("alpha", {})
    b = repo.enqueue("beta", {})
    got = repo.claim("w", ["beta"], 60)
    assert got.id == b.id
    got2 = repo.claim("w", ["beta"], 60)
    assert got2 is None
    got3 = repo.claim("w", ["alpha"], 60)
    assert got3.id == a.id


def test_claim_empty_returns_none(repo):
    assert repo.claim("w", None, 60) is None


def test_stats(repo):
    repo.enqueue("t", {})
    repo.enqueue("t", {})
    c = repo.claim("w", None, 60)
    repo.complete(c.id, "w", {})
    s = repo.stats()
    assert s["pending"] == 1
    assert s["success"] == 1
