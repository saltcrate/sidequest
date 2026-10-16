"""Sidequest task ledger — SQLite, the audit + reputation seed.

Every task is a row: who asked, what was asked, who ran it, what it cost, and
whether it completed. This is the portable-reputation primitive from the plan
(Phase 1: internal ledger; Phase 2: exposure to third-party workers).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any

DEFAULT_DB = os.environ.get(
    "SIDEQUEST_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidequest.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    args            TEXT NOT NULL,
    bounty_cents    INTEGER NOT NULL,
    status          TEXT NOT NULL,          -- pending | executing | done | failed | cancelled
    requester       TEXT,
    worker          TEXT,
    result          TEXT,
    error           TEXT,
    payment_method  TEXT,
    payment_ref     TEXT,
    created         REAL NOT NULL,
    started         REAL,
    completed       REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type    ON tasks(type);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created);
"""


def _conn(db_path: str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(db_path or DEFAULT_DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    t = dict(row)
    for k in ("args", "result"):
        if t.get(k):
            try:
                t[k] = json.loads(t[k])
            except (json.JSONDecodeError, TypeError):
                pass
    return t


def create(
    *,
    task_type: str,
    args: dict[str, Any],
    bounty_cents: int,
    requester: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    task_id = "tq_" + uuid.uuid4().hex[:16]
    now = time.time()
    with _conn(db_path) as db:
        db.execute(
            "INSERT INTO tasks(id,type,args,bounty_cents,status,requester,created)"
            " VALUES(?,?,?,?,?,?,?)",
            (task_id, task_type, json.dumps(args), bounty_cents, "pending", requester, now),
        )
    return get(task_id, db_path=db_path)


def get(task_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    with _conn(db_path) as db:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def list_tasks(
    *, status: str | None = None, task_type: str | None = None, limit: int = 50,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    q = "SELECT * FROM tasks"
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if task_type:
        clauses.append("type=?")
        params.append(task_type)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with _conn(db_path) as db:
        rows = db.execute(q, params).fetchall()
    return [_row_to_task(r) for r in rows]


def claim(task_id: str, worker: str, db_path: str | None = None) -> dict[str, Any] | None:
    """Atomically move pending -> executing. Returns None if not claimable."""
    now = time.time()
    with _conn(db_path) as db:
        cur = db.execute(
            "UPDATE tasks SET status='executing', worker=?, started=? "
            "WHERE id=? AND status='pending'",
            (worker, now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get(task_id, db_path=db_path)


def complete(
    task_id: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    payment_method: str | None = None,
    payment_ref: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    status = "failed" if error else "done"
    with _conn(db_path) as db:
        db.execute(
            "UPDATE tasks SET status=?, result=?, error=?, payment_method=?, payment_ref=?,"
            " completed=? WHERE id=?",
            (
                status,
                json.dumps(result) if result is not None else None,
                error,
                payment_method,
                payment_ref,
                time.time(),
                task_id,
            ),
        )
    return get(task_id, db_path=db_path)


def cancel(task_id: str, db_path: str | None = None) -> bool:
    with _conn(db_path) as db:
        cur = db.execute(
            "UPDATE tasks SET status='cancelled', completed=? "
            "WHERE id=? AND status='pending'",
            (time.time(), task_id),
        )
    return cur.rowcount > 0


def stats(db_path: str | None = None) -> dict[str, Any]:
    """Public reputation/health numbers — completed tasks, gross, by type."""
    with _conn(db_path) as db:
        total = db.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        rows = db.execute(
            "SELECT status, COUNT(*) c, COALESCE(SUM(bounty_cents),0) s FROM tasks GROUP BY status"
        ).fetchall()
        by_type = db.execute(
            "SELECT type, COUNT(*) c, COALESCE(SUM(bounty_cents),0) s FROM tasks"
            " WHERE status='done' GROUP BY type ORDER BY c DESC"
        ).fetchall()
        gross = db.execute(
            "SELECT COALESCE(SUM(bounty_cents),0) s FROM tasks WHERE status='done'"
        ).fetchone()["s"]
        workers = db.execute(
            "SELECT worker, COUNT(*) c FROM tasks WHERE worker IS NOT NULL GROUP BY worker"
        ).fetchall()
    return {
        "tasks_total": total,
        "by_status": {r["status"]: {"count": r["c"], "bounty_cents": r["s"]} for r in rows},
        "by_type_done": {r["type"]: {"count": r["c"], "bounty_cents": r["s"]} for r in by_type},
        "gross_bounty_cents": gross,
        "workers": {r["worker"]: r["c"] for r in workers},
    }
