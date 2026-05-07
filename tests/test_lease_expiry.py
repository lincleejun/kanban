from datetime import datetime, timedelta, timezone


def test_stale_claim_recovery_requeues_expired_claim(repo):
    task = repo.create_task("Lease me", max_attempts=3)
    repo.claim("w1", None, lease_seconds=1)

    future = datetime.now(timezone.utc) + timedelta(seconds=5)
    count = repo.release_stale_claims(future)

    assert count == 1
    after = repo.get(task.id)
    assert after.status == "ready"
    assert after.claim_lock is None
    assert after.claim_expires_at is None
    assert after.consecutive_failures == 1


def test_stale_claim_recovery_blocks_when_attempts_exhausted(repo):
    task = repo.create_task("Lease me", max_attempts=1)
    repo.claim("w1", None, lease_seconds=1)

    repo.release_stale_claims(datetime.now(timezone.utc) + timedelta(seconds=5))

    assert repo.get(task.id).status == "blocked"


def test_stale_claim_recovery_does_not_touch_unexpired(repo):
    repo.create_task("Lease me")
    repo.claim("w1", None, lease_seconds=300)

    assert repo.release_stale_claims(datetime.now(timezone.utc)) == 0


def test_heartbeat_extends_lease(repo):
    task = repo.create_task("Lease me")
    repo.claim("w1", None, lease_seconds=1)
    heartbeat = repo.heartbeat(task.id, "w1", lease_seconds=300)

    assert heartbeat is not None
    count = repo.release_stale_claims(datetime.now(timezone.utc) + timedelta(seconds=10))
    assert count == 0


def test_heartbeat_wrong_worker_rejected(repo):
    task = repo.create_task("Lease me")
    repo.claim("w1", None, 60)

    assert repo.heartbeat(task.id, "w2", 60) is None
