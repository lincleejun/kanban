from datetime import datetime, timedelta, timezone


def test_sweep_requeues_expired(repo):
    t = repo.enqueue("t", {}, max_attempts=3)
    repo.claim("w1", None, lease_seconds=1)

    future = datetime.now(timezone.utc) + timedelta(seconds=5)
    n = repo.sweep_expired(future)
    assert n == 1

    after = repo.get(t.id)
    assert after.status == "pending"
    assert after.worker_id is None
    assert after.lease_expires_at is None
    assert after.attempts == 1  # claim incremented; sweep does not re-increment


def test_sweep_marks_failed_when_attempts_exhausted(repo):
    t = repo.enqueue("t", {}, max_attempts=1)
    repo.claim("w1", None, lease_seconds=1)
    future = datetime.now(timezone.utc) + timedelta(seconds=5)
    repo.sweep_expired(future)
    after = repo.get(t.id)
    assert after.status == "failed"


def test_sweep_does_not_touch_unexpired(repo):
    repo.enqueue("t", {})
    repo.claim("w1", None, lease_seconds=300)
    n = repo.sweep_expired(datetime.now(timezone.utc))
    assert n == 0


def test_heartbeat_extends_lease(repo):
    t = repo.enqueue("t", {})
    repo.claim("w1", None, lease_seconds=1)
    hb = repo.heartbeat(t.id, "w1", lease_seconds=300)
    assert hb is not None

    # Sweep slightly in the future — lease should still be valid.
    n = repo.sweep_expired(datetime.now(timezone.utc) + timedelta(seconds=10))
    assert n == 0


def test_heartbeat_wrong_worker_rejected(repo):
    t = repo.enqueue("t", {})
    repo.claim("w1", None, 60)
    assert repo.heartbeat(t.id, "w2", 60) is None
