import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import Task


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  type             TEXT    NOT NULL,
  payload          TEXT    NOT NULL,
  status           TEXT    NOT NULL CHECK (status IN ('pending','running','success','failed')),
  priority         INTEGER NOT NULL DEFAULT 0,
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 3,
  worker_id        TEXT,
  lease_expires_at TEXT,
  result           TEXT,
  created_at       TEXT    NOT NULL,
  updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(status, type, priority DESC, id);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(status, lease_expires_at);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        type=row["type"],
        payload=json.loads(row["payload"]) if row["payload"] else {},
        status=row["status"],
        priority=row["priority"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        worker_id=row["worker_id"],
        lease_expires_at=_parse_dt(row["lease_expires_at"]),
        result=json.loads(row["result"]) if row["result"] else None,
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


class SqliteTaskRepository:
    """Thread-safe SQLite-backed task repository.

    Uses one connection per thread (sqlite3 connections aren't safe to share
    across threads by default). All write methods open IMMEDIATE transactions
    to make claim races deterministic.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        # Initialize schema on a temporary connection.
        with self._new_conn() as conn:
            conn.executescript(SCHEMA)

    # --- connection management ---

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._new_conn()
            self._local.conn = c
        return c

    # --- mutations ---

    def enqueue(
        self,
        type: str,
        payload: dict[str, Any],
        priority: int = 0,
        max_attempts: int = 3,
    ) -> Task:
        now = _iso(_utcnow())
        conn = self._conn()
        cur = conn.execute(
            """
            INSERT INTO tasks (type, payload, status, priority, attempts,
                               max_attempts, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, 0, ?, ?, ?)
            RETURNING *
            """,
            (type, json.dumps(payload), priority, max_attempts, now, now),
        )
        row = cur.fetchone()
        return _row_to_task(row)

    def claim(
        self,
        worker_id: str,
        types: list[str] | None,
        lease_seconds: int,
    ) -> Task | None:
        now = _utcnow()
        lease_expires = now + timedelta(seconds=lease_seconds)
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if types:
                placeholders = ",".join("?" * len(types))
                select_sql = (
                    f"SELECT id FROM tasks WHERE status = 'pending' "
                    f"AND type IN ({placeholders}) "
                    f"ORDER BY priority DESC, id ASC LIMIT 1"
                )
                params: tuple[Any, ...] = tuple(types)
            else:
                select_sql = (
                    "SELECT id FROM tasks WHERE status = 'pending' "
                    "ORDER BY priority DESC, id ASC LIMIT 1"
                )
                params = ()
            row = conn.execute(select_sql, params).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            task_id = row["id"]
            updated = conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    worker_id = ?,
                    attempts = attempts + 1,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                RETURNING *
                """,
                (worker_id, _iso(lease_expires), _iso(now), task_id),
            ).fetchone()
            conn.execute("COMMIT")
            return _row_to_task(updated) if updated else None
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def heartbeat(
        self, task_id: int, worker_id: str, lease_seconds: int
    ) -> Task | None:
        now = _utcnow()
        lease_expires = now + timedelta(seconds=lease_seconds)
        conn = self._conn()
        row = conn.execute(
            """
            UPDATE tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE id = ? AND worker_id = ? AND status = 'running'
            RETURNING *
            """,
            (_iso(lease_expires), _iso(now), task_id, worker_id),
        ).fetchone()
        return _row_to_task(row) if row else None

    def complete(
        self, task_id: int, worker_id: str, result: Any
    ) -> Task | None:
        now = _iso(_utcnow())
        conn = self._conn()
        row = conn.execute(
            """
            UPDATE tasks
            SET status = 'success',
                result = ?,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE id = ? AND worker_id = ? AND status = 'running'
            RETURNING *
            """,
            (json.dumps(result), now, task_id, worker_id),
        ).fetchone()
        return _row_to_task(row) if row else None

    def fail(
        self, task_id: int, worker_id: str, error: str, requeue: bool
    ) -> Task | None:
        now = _iso(_utcnow())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND worker_id = ? AND status = 'running'",
                (task_id, worker_id),
            )
            row = cur.fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            attempts = row["attempts"]
            max_attempts = row["max_attempts"]
            err_payload = json.dumps({"error": error})
            if requeue and attempts < max_attempts:
                updated = conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'pending',
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        result = ?,
                        updated_at = ?
                    WHERE id = ?
                    RETURNING *
                    """,
                    (err_payload, now, task_id),
                ).fetchone()
            else:
                updated = conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed',
                        lease_expires_at = NULL,
                        result = ?,
                        updated_at = ?
                    WHERE id = ?
                    RETURNING *
                    """,
                    (err_payload, now, task_id),
                ).fetchone()
            conn.execute("COMMIT")
            return _row_to_task(updated) if updated else None
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def sweep_expired(self, now: datetime) -> int:
        """Reclaim tasks whose lease has expired.

        Tasks with attempts < max_attempts go back to pending; otherwise they
        are marked failed.
        """
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, attempts, max_attempts FROM tasks
                WHERE status = 'running' AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (_iso(now),),
            ).fetchall()
            count = 0
            err_msg = json.dumps({"error": "lease expired"})
            now_s = _iso(now)
            for r in rows:
                if r["attempts"] < r["max_attempts"]:
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'pending',
                            worker_id = NULL,
                            lease_expires_at = NULL,
                            result = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (err_msg, now_s, r["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'failed',
                            lease_expires_at = NULL,
                            result = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (json.dumps({"error": "lease expired, attempts exhausted"}),
                         now_s, r["id"]),
                    )
                count += 1
            conn.execute("COMMIT")
            return count
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # --- reads ---

    def get(self, task_id: int) -> Task | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_task(row) if row else None

    def list(
        self,
        status: str | None,
        type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Task], int]:
        conn = self._conn()
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if type:
            clauses.append("type = ?")
            params.append(type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM tasks{where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM tasks{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [_row_to_task(r) for r in rows], total

    def stats(self) -> dict[str, int]:
        conn = self._conn()
        out = {"pending": 0, "running": 0, "success": 0, "failed": 0}
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"
        ):
            out[r["status"]] = r["c"]
        return out
