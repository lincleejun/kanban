import threading

from app.sqlite_repo import SqliteTaskRepository


def test_concurrent_claims_each_task_claimed_once(tmp_path):
    db = tmp_path / "concur.db"
    repo = SqliteTaskRepository(str(db))

    n_tasks = 50
    n_workers = 10
    for i in range(n_tasks):
        repo.create_task(f"task {i}", type="t", payload={"i": i})

    claimed_ids: list[str] = []
    lock = threading.Lock()

    def worker(name: str):
        # Each thread needs its own repo (its own thread-local connection).
        local_repo = SqliteTaskRepository(str(db))
        while True:
            t = local_repo.claim(name, None, lease_seconds=60)
            if t is None:
                return
            with lock:
                claimed_ids.append(t.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(n_workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(claimed_ids) == n_tasks
    assert len(set(claimed_ids)) == n_tasks  # no duplicates
