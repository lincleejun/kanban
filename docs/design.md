# Kanban Core — Design

A small, durable task queue for multi-agent workers. SQLite + FastAPI,
no UI, no gateway, no broker. The only thing it does is hold the task
graph and enforce safe handoff between workers.

## Goals

1. **Simple to deploy** — one process, one SQLite file. No external services.
2. **Safe under crashes** — workers that die mid-task don't block the queue.
3. **Provider-neutral** — Codex, Claude Code, custom CLIs, or shell scripts
   all use the same claim/heartbeat/complete protocol.
4. **Auditable** — every state change is an event row.

## Non-goals

- No browser dashboard, no gateway/reverse-proxy, no provider-specific worker
  shims. Those belong in the orchestration layer that drives this service.
- No multi-tenant authn/authz. The service trusts callers; put it behind a
  reverse proxy or VPN if you need access control.

## Core concepts

### Task

The durable unit of work. A task has:

- `title`, `body` — human-readable instruction.
- `type`, `payload` — machine-readable routing data.
- `assignee` — optional worker class (`codex`, `claude`, …).
- `priority` — higher first.
- `workspace_kind`, `workspace_path` — where the worker should operate.
- `claim_lock`, `claim_expires_at`, `current_run_id` — lease state.
- `result`, `last_failure_error`, `consecutive_failures` — outcome state.

### Status state machine

```
triage ──► todo ──► ready ──► running ──► done ──► archived
            │         ▲          │
            │         └── retry ─┘
            └──────► blocked ◄───┘   (manual or attempts exhausted)
```

- `todo` — exists but waiting on parent tasks.
- `ready` — all parents `done`; eligible for claim.
- `running` — held by a worker. **Always** has a non-null lease.
- `blocked` — manual hold or retries exhausted. Reset with `unblock`.
- `done` / `archived` — terminal.

The transition rules are enforced in code, not only by clients:

- A task only enters `running` via `claim` (which atomically sets the lease).
- A task only enters `done` via `complete` (which requires the holding worker).
- `set_status` cannot target `running` or `done`, and cannot leave `running`
  or `archived`. This preserves the lease invariant.

### Task links

`task_links(parent_id, child_id)` form a DAG. Cycles are rejected at link
time. A child becomes `ready` only when **all** parents are `done`.

When a new unfinished parent is linked to an already-`ready` child, the
child is demoted to `todo`.

### Runs

Every claim opens a `task_runs` row (`worker_id`, `started_at`,
`heartbeat_at`, `ended_at`, `status`, `error`, `summary`, `metadata`). The
task row tells the scheduler what's next; the run row tells operators what
actually happened.

### Comments and events

`task_comments` are explicit collaboration notes between agents and humans.
`task_events` is the append-only audit log: `created`, `linked`, `claimed`,
`heartbeat`, `completed`, `failed`, `blocked`, `unblocked`, `archived`,
`claim_expired`, `became_ready`, `status_changed`.

`build_worker_context(task_id)` returns a consistent snapshot (read in a
single transaction) containing the task, its parents and children, recent
events, comments, and runs — the handoff packet a worker reads before
starting.

## Scheduling

`claim(worker_id, types, lease_seconds, assignee=, tenant=)`:

1. `BEGIN IMMEDIATE` — exclusive write lock so concurrent workers cannot
   race for the same task.
2. Select one task with `status = 'ready' AND claim_lock IS NULL`,
   filtered by `types` / `assignee` / `tenant`.
3. Order by `priority DESC, created_at ASC` (FIFO within a priority).
4. Update to `running`, set the lease, open a run row.
5. Commit.

**Ready computation is incremental.** When a parent transitions to `done`,
only its direct children are checked for promotion. `claim` does not scan
the whole table — that would serialize all workers behind every poll.
A full sweep is available as `recompute_ready()` for recovery.

## Lease recovery

The sweeper runs every `SWEEP_INTERVAL_SECONDS` and calls
`release_stale_claims`:

- Find all `running` tasks with `claim_expires_at < now`.
- Bulk-update those whose `consecutive_failures + 1 < max_attempts` back
  to `ready`; the rest go to `blocked`.
- Close their active run rows as `expired`.
- Emit `claim_expired` events.

This is the mechanism that lets external CLI workers crash without losing
work. Heartbeats only need to fire often enough to stay inside the lease
window.

## Concurrency model

- One SQLite connection per Python thread (`threading.local`), opened in
  WAL mode with autocommit + manual `BEGIN IMMEDIATE`.
- Connection cache invalidates on hard errors (rolled-back-and-failed,
  closed connections) so a transient `database is locked` cannot poison
  a worker thread.
- `_read()` wraps multi-query reads in a single transaction so the worker
  context snapshot is consistent across tables.

## Error model (HTTP)

| Status | Meaning |
|--------|---------|
| `204`  | `claim` had no eligible task. |
| `404`  | Task / link not found. |
| `409`  | Lease/state mismatch — wrong worker, not running, wrong source state. |
| `422`  | Invalid input (bad status target, lease ≤ 0, oversized field). |

## Schema versioning

`PRAGMA user_version` carries the schema revision. New revisions add an
`ALTER TABLE` block in `_migrate`. The schema itself uses
`CREATE … IF NOT EXISTS` for idempotent first-run.

## Attaching Codex or Claude Code

The kanban core does **not** spawn external processes. A worker (a
separate process you write) does this:

1. Claim a task with `assignee=codex` (or `claude`, etc.).
2. `GET /tasks/{id}/context` for the handoff packet.
3. Start the chosen platform with that context as input.
4. Heartbeat while it runs.
5. Call `complete`, `fail`, or `block`.

Different roles can use different providers without changing this core.
