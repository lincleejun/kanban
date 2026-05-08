"""SQLite-backed kanban core.

Single-file repository with atomic claims, worker leases, dependency-driven
readiness, and per-run history. Designed to be small, auditable, and safe
under concurrent worker access.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .models import TASK_STATUSES, Task, TaskComment, TaskEvent, TaskRun

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id                   TEXT PRIMARY KEY,
  title                TEXT NOT NULL,
  body                 TEXT NOT NULL DEFAULT '',
  status               TEXT NOT NULL CHECK (status IN ('triage','todo','ready','running','blocked','done','archived')),
  priority             INTEGER NOT NULL DEFAULT 0,
  assignee             TEXT,
  created_by           TEXT NOT NULL DEFAULT 'user',
  type                 TEXT,
  payload              TEXT NOT NULL DEFAULT '{}',
  workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
  workspace_path       TEXT,
  tenant               TEXT,
  idempotency_key      TEXT UNIQUE,
  claim_lock           TEXT,
  claim_expires_at     TEXT,
  last_heartbeat_at    TEXT,
  current_run_id       TEXT,
  max_attempts         INTEGER NOT NULL DEFAULT 3,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_failure_error   TEXT,
  max_runtime_seconds  INTEGER,
  result               TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  started_at           TEXT,
  completed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(status, assignee, type, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(status, claim_expires_at);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant, status);

CREATE TABLE IF NOT EXISTS task_links (
  parent_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  child_id   TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY (parent_id, child_id),
  CHECK (parent_id <> child_id)
);
CREATE INDEX IF NOT EXISTS idx_task_links_child ON task_links(child_id);

CREATE TABLE IF NOT EXISTS task_comments (
  id         TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  author     TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id, created_at);

CREATE TABLE IF NOT EXISTS task_events (
  id         TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  actor      TEXT,
  data       TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);

CREATE TABLE IF NOT EXISTS task_runs (
  id           TEXT PRIMARY KEY,
  task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  worker_id    TEXT NOT NULL,
  status       TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  heartbeat_at TEXT,
  error        TEXT,
  summary      TEXT,
  metadata     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, started_at);
"""

# Manual-only target statuses for set_status. running is excluded — it must
# go through claim() so a lease is always acquired atomically.
MANUAL_STATUS_TARGETS = frozenset({"triage", "todo", "ready", "blocked", "archived"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _json_loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        status=row["status"],
        priority=row["priority"],
        assignee=row["assignee"],
        created_by=row["created_by"],
        type=row["type"],
        payload=_json_loads(row["payload"], {}),
        workspace_kind=row["workspace_kind"],
        workspace_path=row["workspace_path"],
        tenant=row["tenant"],
        idempotency_key=row["idempotency_key"],
        claim_lock=row["claim_lock"],
        claim_expires_at=_parse_dt(row["claim_expires_at"]),
        last_heartbeat_at=_parse_dt(row["last_heartbeat_at"]),
        current_run_id=row["current_run_id"],
        max_attempts=row["max_attempts"],
        consecutive_failures=row["consecutive_failures"],
        last_failure_error=row["last_failure_error"],
        max_runtime_seconds=row["max_runtime_seconds"],
        result=_json_loads(row["result"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        started_at=_parse_dt(row["started_at"]),
        completed_at=_parse_dt(row["completed_at"]),
    )


def _row_to_run(row: sqlite3.Row) -> TaskRun:
    return TaskRun(
        id=row["id"],
        task_id=row["task_id"],
        worker_id=row["worker_id"],
        status=row["status"],
        started_at=_parse_dt(row["started_at"]),
        ended_at=_parse_dt(row["ended_at"]),
        heartbeat_at=_parse_dt(row["heartbeat_at"]),
        error=row["error"],
        summary=row["summary"],
        metadata=_json_loads(row["metadata"], {}),
    )


def _row_to_comment(row: sqlite3.Row) -> TaskComment:
    return TaskComment(
        id=row["id"],
        task_id=row["task_id"],
        author=row["author"],
        body=row["body"],
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        id=row["id"],
        task_id=row["task_id"],
        event_type=row["event_type"],
        actor=row["actor"],
        data=_json_loads(row["data"], {}),
        created_at=_parse_dt(row["created_at"]),
    )


class SqliteTaskRepository:
    """SQLite-backed kanban core for agent orchestration."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        with self._new_conn() as conn:
            self._migrate(conn)

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def _drop_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _write(self):
        """Acquire BEGIN IMMEDIATE; on hard connection errors, drop the
        cached thread-local connection so the next call reconnects cleanly."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            self._drop_conn()
            raise
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                # Connection may be in an unrecoverable state — drop it so
                # the next operation gets a fresh connection.
                self._drop_conn()
            raise

    @contextmanager
    def _read(self):
        """Read transaction for consistent multi-query snapshots."""
        conn = self._conn()
        try:
            conn.execute("BEGIN")
        except sqlite3.Error:
            self._drop_conn()
            raise
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                self._drop_conn()
            raise

    def _event(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        event_type: str,
        actor: str | None,
        data: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        ts = _iso(now or _utcnow())
        conn.execute(
            "INSERT INTO task_events (id, task_id, event_type, actor, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_id("evt"), task_id, event_type, actor, _json_dumps(data or {}), ts),
        )

    def _get_in_txn(self, conn: sqlite3.Connection, task_id: str) -> Task | None:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------

    def create_task(
        self,
        title: str,
        body: str = "",
        *,
        assignee: str | None = None,
        created_by: str = "user",
        type: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        status: str | None = None,
        parents: list[str] | None = None,
        workspace_kind: str = "scratch",
        workspace_path: str | None = None,
        tenant: str | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        max_runtime_seconds: int | None = None,
    ) -> Task:
        now = _utcnow()
        parents = parents or []
        initial_status = status or ("todo" if parents else "ready")
        if initial_status not in TASK_STATUSES:
            raise ValueError(f"invalid status: {initial_status}")
        if initial_status == "running":
            raise ValueError("cannot create a task directly in 'running'; use claim()")

        try:
            with self._write() as conn:
                if idempotency_key:
                    existing = conn.execute(
                        "SELECT * FROM tasks WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if existing:
                        return _row_to_task(existing)
                task_id = _id("task")
                row = conn.execute(
                    """
                    INSERT INTO tasks (
                      id, title, body, status, priority, assignee, created_by, type,
                      payload, workspace_kind, workspace_path, tenant, idempotency_key,
                      max_attempts, max_runtime_seconds, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING *
                    """,
                    (
                        task_id,
                        title,
                        body,
                        initial_status,
                        priority,
                        assignee,
                        created_by,
                        type,
                        _json_dumps(payload or {}),
                        workspace_kind,
                        workspace_path,
                        tenant,
                        idempotency_key,
                        max_attempts,
                        max_runtime_seconds,
                        _iso(now),
                        _iso(now),
                    ),
                ).fetchone()
                for parent_id in parents:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links (parent_id, child_id, created_at) "
                        "VALUES (?, ?, ?)",
                        (parent_id, task_id, _iso(now)),
                    )
                self._event(
                    conn,
                    task_id,
                    "created",
                    created_by,
                    {"status": initial_status, "parents": parents},
                    now,
                )
                # If created as todo with parents, promote to ready iff all parents done.
                if initial_status == "todo" and parents:
                    self._maybe_promote_children_in_txn(conn, [task_id], now)
                return self._get_in_txn(conn, task_id)
        except sqlite3.IntegrityError:
            # Idempotency-key UNIQUE race: another writer inserted between
            # our SELECT and INSERT. Re-read and return the existing task.
            if idempotency_key:
                existing = self._conn().execute(
                    "SELECT * FROM tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return _row_to_task(existing)
            raise

    def claim(
        self,
        worker_id: str,
        types: list[str] | None,
        lease_seconds: int,
        *,
        assignee: str | None = None,
        tenant: str | None = None,
    ) -> Task | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utcnow()
        lease_expires = now + timedelta(seconds=lease_seconds)
        with self._write() as conn:
            clauses = ["status = 'ready'", "claim_lock IS NULL"]
            params: list[Any] = []
            if types:
                clauses.append(f"type IN ({','.join('?' * len(types))})")
                params.extend(types)
            if assignee:
                clauses.append("(assignee IS NULL OR assignee = ?)")
                params.append(assignee)
            if tenant:
                clauses.append("tenant = ?")
                params.append(tenant)
            where = " AND ".join(clauses)
            selected = conn.execute(
                f"SELECT id FROM tasks WHERE {where} "
                "ORDER BY priority DESC, created_at ASC LIMIT 1",
                params,
            ).fetchone()
            if selected is None:
                return None
            run_id = _id("run")
            row = conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    claim_lock = ?,
                    claim_expires_at = ?,
                    last_heartbeat_at = ?,
                    current_run_id = ?,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE id = ? AND status = 'ready' AND claim_lock IS NULL
                RETURNING *
                """,
                (
                    worker_id,
                    _iso(lease_expires),
                    _iso(now),
                    run_id,
                    _iso(now),
                    _iso(now),
                    selected["id"],
                ),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "INSERT INTO task_runs (id, task_id, worker_id, status, started_at, heartbeat_at, metadata) "
                "VALUES (?, ?, ?, 'running', ?, ?, '{}')",
                (run_id, row["id"], worker_id, _iso(now), _iso(now)),
            )
            self._event(conn, row["id"], "claimed", worker_id, {"run_id": run_id}, now)
            return _row_to_task(row)

    def heartbeat(
        self, task_id: str, worker_id: str, lease_seconds: int, note: str | None = None
    ) -> Task | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utcnow()
        lease_expires = now + timedelta(seconds=lease_seconds)
        with self._write() as conn:
            row = conn.execute(
                "UPDATE tasks SET claim_expires_at = ?, last_heartbeat_at = ?, updated_at = ? "
                "WHERE id = ? AND claim_lock = ? AND status = 'running' RETURNING *",
                (_iso(lease_expires), _iso(now), _iso(now), task_id, worker_id),
            ).fetchone()
            if row is None:
                return None
            if row["current_run_id"]:
                conn.execute(
                    "UPDATE task_runs SET heartbeat_at = ? WHERE id = ?",
                    (_iso(now), row["current_run_id"]),
                )
            self._event(conn, task_id, "heartbeat", worker_id, {"note": note}, now)
            return _row_to_task(row)

    def complete(
        self,
        task_id: str,
        worker_id: str,
        result: Any = None,
        *,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task | None:
        now = _utcnow()
        with self._write() as conn:
            current = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND claim_lock = ? AND status = 'running'",
                (task_id, worker_id),
            ).fetchone()
            if current is None:
                return None
            row = conn.execute(
                """
                UPDATE tasks
                SET status = 'done',
                    result = ?,
                    claim_lock = NULL,
                    claim_expires_at = NULL,
                    last_heartbeat_at = NULL,
                    consecutive_failures = 0,
                    current_run_id = NULL,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                RETURNING *
                """,
                (_json_dumps(result), _iso(now), _iso(now), task_id),
            ).fetchone()
            if current["current_run_id"]:
                conn.execute(
                    "UPDATE task_runs SET status = 'done', ended_at = ?, summary = ?, metadata = ? "
                    "WHERE id = ?",
                    (
                        _iso(now),
                        summary,
                        _json_dumps(metadata or {}),
                        current["current_run_id"],
                    ),
                )
            self._event(conn, task_id, "completed", worker_id, {"summary": summary}, now)
            # Only completing a task can newly satisfy children; recompute just those.
            child_ids = [
                r["child_id"]
                for r in conn.execute(
                    "SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,)
                )
            ]
            if child_ids:
                self._maybe_promote_children_in_txn(conn, child_ids, now)
            return _row_to_task(row)

    def fail(self, task_id: str, worker_id: str, error: str, requeue: bool) -> Task | None:
        now = _utcnow()
        with self._write() as conn:
            current = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND claim_lock = ? AND status = 'running'",
                (task_id, worker_id),
            ).fetchone()
            if current is None:
                return None
            failures = current["consecutive_failures"] + 1
            status = "ready" if requeue and failures < current["max_attempts"] else "blocked"
            row = conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    claim_lock = NULL,
                    claim_expires_at = NULL,
                    last_heartbeat_at = NULL,
                    current_run_id = NULL,
                    consecutive_failures = ?,
                    last_failure_error = ?,
                    updated_at = ?
                WHERE id = ?
                RETURNING *
                """,
                (status, failures, error, _iso(now), task_id),
            ).fetchone()
            if current["current_run_id"]:
                conn.execute(
                    "UPDATE task_runs SET status = 'failed', ended_at = ?, error = ? WHERE id = ?",
                    (_iso(now), error, current["current_run_id"]),
                )
            self._event(
                conn,
                task_id,
                "failed",
                worker_id,
                {"error": error, "next_status": status},
                now,
            )
            return _row_to_task(row)

    def block(self, task_id: str, actor: str, reason: str) -> Task | None:
        now = _utcnow()
        with self._write() as conn:
            current = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND status NOT IN ('done', 'archived')",
                (task_id,),
            ).fetchone()
            if current is None:
                return None
            # If blocking a running task, close out the active run too.
            if current["status"] == "running" and current["current_run_id"]:
                conn.execute(
                    "UPDATE task_runs SET status = 'blocked', ended_at = ?, error = ? "
                    "WHERE id = ? AND ended_at IS NULL",
                    (_iso(now), reason, current["current_run_id"]),
                )
            row = conn.execute(
                """
                UPDATE tasks
                SET status = 'blocked',
                    claim_lock = NULL,
                    claim_expires_at = NULL,
                    last_heartbeat_at = NULL,
                    current_run_id = NULL,
                    last_failure_error = ?,
                    updated_at = ?
                WHERE id = ?
                RETURNING *
                """,
                (reason, _iso(now), task_id),
            ).fetchone()
            self._event(conn, task_id, "blocked", actor, {"reason": reason}, now)
            return _row_to_task(row)

    def unblock(self, task_id: str, actor: str = "user") -> Task | None:
        """Move a blocked task back into the queue. Returns to `ready` if all
        parents are done, otherwise to `todo`. Resets the failure counter."""
        now = _utcnow()
        with self._write() as conn:
            current = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND status = 'blocked'",
                (task_id,),
            ).fetchone()
            if current is None:
                return None
            target = "ready" if self._parents_all_done(conn, task_id) else "todo"
            row = conn.execute(
                "UPDATE tasks SET status = ?, consecutive_failures = 0, last_failure_error = NULL, "
                "updated_at = ? WHERE id = ? RETURNING *",
                (target, _iso(now), task_id),
            ).fetchone()
            self._event(
                conn, task_id, "unblocked", actor, {"next_status": target}, now
            )
            return _row_to_task(row)

    def archive(self, task_id: str, actor: str = "user") -> Task | None:
        """Archive a `done` task. Terminal, idempotent."""
        now = _utcnow()
        with self._write() as conn:
            row = conn.execute(
                "UPDATE tasks SET status = 'archived', updated_at = ? "
                "WHERE id = ? AND status = 'done' RETURNING *",
                (_iso(now), task_id),
            ).fetchone()
            if row is None:
                return None
            self._event(conn, task_id, "archived", actor, {}, now)
            return _row_to_task(row)

    def set_status(self, task_id: str, status: str, actor: str = "user") -> Task | None:
        """Manual status override. Restricted to safe transitions:

        - cannot target `running` (must go through claim)
        - cannot target `done` (must go through complete)
        - cannot leave `running` (must go through complete/fail/block)
        - cannot leave `archived` (terminal)
        """
        if status not in MANUAL_STATUS_TARGETS:
            raise ValueError(f"status not allowed via set_status: {status}")
        now = _utcnow()
        with self._write() as conn:
            current = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if current is None:
                return None
            if current["status"] == status:
                return _row_to_task(current)
            if current["status"] == "running":
                raise ValueError(
                    "cannot leave 'running' via set_status; use complete/fail/block"
                )
            if current["status"] == "archived":
                raise ValueError("cannot transition out of 'archived'")
            row = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? RETURNING *",
                (status, _iso(now), task_id),
            ).fetchone()
            self._event(
                conn,
                task_id,
                "status_changed",
                actor,
                {"from": current["status"], "to": status},
                now,
            )
            # Moving to todo may need promotion; moving to ready never blocks.
            if status == "todo":
                self._maybe_promote_children_in_txn(conn, [task_id], now)
            return _row_to_task(row)

    def add_comment(self, task_id: str, author: str, body: str) -> TaskComment | None:
        now = _utcnow()
        with self._write() as conn:
            if self._get_in_txn(conn, task_id) is None:
                return None
            row = conn.execute(
                "INSERT INTO task_comments (id, task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?, ?) RETURNING *",
                (_id("cmt"), task_id, author, body, _iso(now)),
            ).fetchone()
            self._event(conn, task_id, "commented", author, {}, now)
            return _row_to_comment(row)

    def link_tasks(self, parent_id: str, child_id: str, actor: str = "user") -> bool:
        now = _utcnow()
        with self._write() as conn:
            parent = self._get_in_txn(conn, parent_id)
            child = self._get_in_txn(conn, child_id)
            if parent is None or child is None or parent_id == child_id:
                return False
            # Cycle check: would parent be reachable from child?
            if self._has_path(conn, child_id, parent_id):
                return False
            cur = conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id, created_at) "
                "VALUES (?, ?, ?)",
                (parent_id, child_id, _iso(now)),
            )
            if cur.rowcount == 0:
                return True  # link already existed; idempotent
            # New unfinished parent on a ready child -> demote child to todo.
            if parent.status != "done" and child.status == "ready":
                conn.execute(
                    "UPDATE tasks SET status = 'todo', updated_at = ? WHERE id = ?",
                    (_iso(now), child_id),
                )
            self._event(conn, child_id, "linked", actor, {"parent_id": parent_id}, now)
            return True

    def unlink_tasks(self, parent_id: str, child_id: str, actor: str = "user") -> bool:
        now = _utcnow()
        with self._write() as conn:
            cur = conn.execute(
                "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
                (parent_id, child_id),
            )
            if cur.rowcount == 0:
                return False
            self._event(conn, child_id, "unlinked", actor, {"parent_id": parent_id}, now)
            self._maybe_promote_children_in_txn(conn, [child_id], now)
            return True

    def release_stale_claims(self, now: datetime | None = None) -> int:
        """Sweep expired leases. Returns the number of tasks released.

        Tasks below max_attempts return to `ready`; the rest move to `blocked`.
        Active runs are closed as `expired`."""
        now = now or _utcnow()
        now_iso = _iso(now)
        error = "claim expired"
        with self._write() as conn:
            rows = conn.execute(
                "SELECT id, current_run_id, consecutive_failures, max_attempts "
                "FROM tasks "
                "WHERE status = 'running' "
                "  AND claim_expires_at IS NOT NULL "
                "  AND claim_expires_at < ?",
                (now_iso,),
            ).fetchall()
            if not rows:
                return 0
            ready_ids: list[str] = []
            blocked_ids: list[str] = []
            run_ids: list[str] = []
            for row in rows:
                next_failures = row["consecutive_failures"] + 1
                target = ready_ids if next_failures < row["max_attempts"] else blocked_ids
                target.append(row["id"])
                if row["current_run_id"]:
                    run_ids.append(row["current_run_id"])
            self._bulk_release(conn, ready_ids, "ready", error, now_iso)
            self._bulk_release(conn, blocked_ids, "blocked", error, now_iso)
            for rid in run_ids:
                conn.execute(
                    "UPDATE task_runs SET status = 'expired', ended_at = ?, error = ? "
                    "WHERE id = ?",
                    (now_iso, error, rid),
                )
            for tid in ready_ids:
                self._event(conn, tid, "claim_expired", "sweeper", {"next_status": "ready"}, now)
            for tid in blocked_ids:
                self._event(conn, tid, "claim_expired", "sweeper", {"next_status": "blocked"}, now)
            return len(rows)

    def _bulk_release(
        self,
        conn: sqlite3.Connection,
        ids: list[str],
        status: str,
        error: str,
        now_iso: str,
    ) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"""
            UPDATE tasks
            SET status = ?,
                claim_lock = NULL,
                claim_expires_at = NULL,
                last_heartbeat_at = NULL,
                current_run_id = NULL,
                consecutive_failures = consecutive_failures + 1,
                last_failure_error = ?,
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (status, error, now_iso, *ids),
        )

    def recompute_ready(self) -> int:
        """Full sweep: promote any todo whose parents are all done. Use
        sparingly — normal flow promotes incrementally on complete()."""
        now = _utcnow()
        with self._write() as conn:
            rows = conn.execute(
                """
                SELECT t.id FROM tasks t
                WHERE t.status = 'todo'
                  AND NOT EXISTS (
                    SELECT 1 FROM task_links l
                    JOIN tasks p ON p.id = l.parent_id
                    WHERE l.child_id = t.id AND p.status != 'done'
                  )
                """
            ).fetchall()
            ids = [r["id"] for r in rows]
            self._promote_to_ready(conn, ids, now)
            return len(ids)

    def _maybe_promote_children_in_txn(
        self, conn: sqlite3.Connection, candidate_ids: Iterable[str], now: datetime
    ) -> None:
        """For each candidate, if it's `todo` and all parents are `done`,
        promote it to `ready`."""
        ready: list[str] = []
        for cid in candidate_ids:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (cid,)
            ).fetchone()
            if row is None or row["status"] != "todo":
                continue
            if self._parents_all_done(conn, cid):
                ready.append(cid)
        self._promote_to_ready(conn, ready, now)

    def _promote_to_ready(
        self, conn: sqlite3.Connection, ids: list[str], now: datetime
    ) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE tasks SET status = 'ready', updated_at = ? "
            f"WHERE id IN ({placeholders}) AND status = 'todo'",
            (_iso(now), *ids),
        )
        for tid in ids:
            self._event(conn, tid, "became_ready", "system", {}, now)

    def _parents_all_done(self, conn: sqlite3.Connection, child_id: str) -> bool:
        row = conn.execute(
            """
            SELECT COUNT(*) AS pending FROM task_links l
            JOIN tasks p ON p.id = l.parent_id
            WHERE l.child_id = ? AND p.status != 'done'
            """,
            (child_id,),
        ).fetchone()
        return row["pending"] == 0

    def _has_path(self, conn: sqlite3.Connection, src: str, dst: str) -> bool:
        """True if dst is reachable from src via parent->child edges."""
        seen: set[str] = set()
        frontier = [src]
        while frontier:
            node = frontier.pop()
            if node == dst:
                return True
            if node in seen:
                continue
            seen.add(node)
            for r in conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
            ):
                frontier.append(r["child_id"])
        return False

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        row = self._conn().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def list(
        self,
        status: str | None,
        type: str | None,
        limit: int,
        offset: int,
        *,
        assignee: str | None = None,
        tenant: str | None = None,
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
        if assignee:
            clauses.append("assignee = ?")
            params.append(assignee)
        if tenant:
            clauses.append("tenant = ?")
            params.append(tenant)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = conn.execute(f"SELECT COUNT(*) AS c FROM tasks{where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM tasks{where} ORDER BY priority DESC, created_at ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [_row_to_task(r) for r in rows], total

    def list_comments(self, task_id: str) -> list[TaskComment]:
        rows = self._conn().execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        return [_row_to_comment(r) for r in rows]

    def list_events(self, task_id: str, limit: int = 100) -> list[TaskEvent]:
        rows = self._conn().execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def list_runs(self, task_id: str, limit: int = 50) -> list[TaskRun]:
        rows = self._conn().execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at ASC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [_row_to_run(r) for r in rows]

    def parents(self, task_id: str) -> list[Task]:
        rows = self._conn().execute(
            "SELECT p.* FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? ORDER BY p.created_at ASC",
            (task_id,),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def children(self, task_id: str) -> list[Task]:
        rows = self._conn().execute(
            "SELECT c.* FROM task_links l JOIN tasks c ON c.id = l.child_id "
            "WHERE l.parent_id = ? ORDER BY c.created_at ASC",
            (task_id,),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def build_worker_context(
        self, task_id: str, *, event_limit: int = 25, run_limit: int = 25
    ) -> dict[str, Any] | None:
        """Snapshot a task and its handoff context atomically."""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            task = _row_to_task(row)
            parents = [
                _row_to_task(r)
                for r in conn.execute(
                    "SELECT p.* FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                    "WHERE l.child_id = ? ORDER BY p.created_at ASC",
                    (task_id,),
                )
            ]
            children = [
                _row_to_task(r)
                for r in conn.execute(
                    "SELECT c.* FROM task_links l JOIN tasks c ON c.id = l.child_id "
                    "WHERE l.parent_id = ? ORDER BY c.created_at ASC",
                    (task_id,),
                )
            ]
            comments = [
                _row_to_comment(r)
                for r in conn.execute(
                    "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
                    (task_id,),
                )
            ]
            events = [
                _row_to_event(r)
                for r in conn.execute(
                    "SELECT * FROM task_events WHERE task_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (task_id, event_limit),
                )
            ]
            events.reverse()
            runs = [
                _row_to_run(r)
                for r in conn.execute(
                    "SELECT * FROM task_runs WHERE task_id = ? "
                    "ORDER BY started_at DESC LIMIT ?",
                    (task_id, run_limit),
                )
            ]
            runs.reverse()
        return {
            "task": task.to_dict(),
            "parents": [t.to_dict() for t in parents],
            "children": [t.to_dict() for t in children],
            "comments": [c.to_dict() for c in comments],
            "recent_events": [e.to_dict() for e in events],
            "runs": [r.to_dict() for r in runs],
        }

    def stats(self) -> dict[str, int]:
        out = {status: 0 for status in TASK_STATUSES}
        for row in self._conn().execute(
            "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"
        ):
            out[row["status"]] = row["c"]
        return out

    def dispatch_once(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        assignee: str | None = None,
    ) -> dict[str, Any]:
        """Sweep stale leases, claim one ready task, and return its context."""
        self.release_stale_claims()
        task = self.claim(worker_id, None, lease_seconds, assignee=assignee)
        if task is None:
            return {"claimed": False, "task": None}
        return {
            "claimed": True,
            "task": task.to_dict(),
            "context": self.build_worker_context(task.id),
        }
