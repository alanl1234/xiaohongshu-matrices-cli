"""Durable queue, schema migration, backup, and persisted account throttling."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .db import Database
from .utils import now_iso

P0_SCHEMA_VERSION = 2

P0_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
 key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_queue (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(kind IN ('search','publish')),
 resource_id INTEGER NOT NULL,
 account_id INTEGER,
 status TEXT NOT NULL DEFAULT 'queued',
 available_at TEXT NOT NULL,
 lease_until TEXT,
 heartbeat_at TEXT,
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 2,
 idempotency_key TEXT NOT NULL UNIQUE,
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(account_id) REFERENCES accounts(id));
CREATE INDEX IF NOT EXISTS idx_task_queue_claim
 ON task_queue(status,available_at,id);
CREATE INDEX IF NOT EXISTS idx_task_queue_account
 ON task_queue(account_id,status);
CREATE TABLE IF NOT EXISTS search_candidates (
 job_id INTEGER NOT NULL,
 note_id TEXT NOT NULL,
 token TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT 'pc_search',
 status TEXT NOT NULL DEFAULT 'pending',
 last_error TEXT,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(job_id,note_id),
 FOREIGN KEY(job_id) REFERENCES search_jobs(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_search_candidates_status
 ON search_candidates(job_id,status);
CREATE TABLE IF NOT EXISTS account_usage (
 account_id INTEGER PRIMARY KEY,
 usage_date TEXT NOT NULL,
 request_count INTEGER NOT NULL DEFAULT 0,
 next_request_at TEXT,
 paused_until TEXT,
 pause_reason TEXT,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE);
"""


@dataclass(frozen=True)
class QueueItem:
    id: int
    kind: str
    resource_id: int
    account_id: int | None
    attempts: int
    max_attempts: int


class DailyLimitReached(RuntimeError):
    pass


class AccountTemporarilyPaused(RuntimeError):
    pass


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class P0Store:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.RLock()
        self._migrate()

    def _backup_if_needed(self, con: sqlite3.Connection, current: int) -> None:
        if current >= P0_SCHEMA_VERSION:
            return
        has_data = any(
            con.execute(f"SELECT EXISTS(SELECT 1 FROM {table} LIMIT 1)").fetchone()[0]
            for table in ("accounts", "search_jobs", "publish_tasks", "notes")
        )
        if not has_data:
            return
        backup_dir = self.db.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        with sqlite3.connect(backup_dir / f"dashboard-before-v{P0_SCHEMA_VERSION}-{stamp}.sqlite3") as target:
            con.backup(target)

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}

    def _add_column(self, con: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in self._columns(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate(self) -> None:
        with self._lock, self.db.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            row = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            current = int(row[0]) if row else 1
            self._backup_if_needed(con, current)
            con.executescript(P0_SCHEMA)
            self._add_column(con, "publish_tasks", "content_fingerprint TEXT NOT NULL DEFAULT ''")
            self._add_column(con, "publish_attempts", "submitted_at TEXT")
            self._add_column(con, "publish_attempts", "error_category TEXT")
            self._add_column(con, "publish_attempts", "before_note_ids_json TEXT NOT NULL DEFAULT '[]'")
            self._add_column(con, "accounts", "profile_acl_status TEXT NOT NULL DEFAULT 'unknown'")
            con.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(P0_SCHEMA_VERSION),),
            )
            con.commit()

    def enqueue(
        self,
        kind: str,
        resource_id: int,
        account_id: int | None,
        *,
        max_attempts: int = 2,
    ) -> int:
        if kind not in {"search", "publish"}:
            raise ValueError("unsupported queue task kind")
        key = f"{kind}:{resource_id}"
        now = now_iso()
        with self._lock, self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT id,status FROM task_queue WHERE idempotency_key=?", (key,)).fetchone()
            if row and row["status"] in {"queued", "running", "retry_wait"}:
                con.commit()
                return int(row["id"])
            if row:
                con.execute(
                    """UPDATE task_queue SET account_id=?,status='queued',available_at=?,lease_until=NULL,
                    heartbeat_at=NULL,attempts=0,max_attempts=?,last_error=NULL,updated_at=? WHERE id=?""",
                    (account_id, now, max_attempts, now, row["id"]),
                )
                queue_id = int(row["id"])
            else:
                cursor = con.execute(
                    """INSERT INTO task_queue(kind,resource_id,account_id,status,available_at,max_attempts,
                    idempotency_key,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?,?,?)""",
                    (kind, resource_id, account_id, now, max_attempts, key, now, now),
                )
                queue_id = int(cursor.lastrowid)
            con.commit()
            return queue_id

    def recover_expired(self) -> None:
        now = now_iso()
        with self._lock, self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT * FROM task_queue WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?",
                (now,),
            ).fetchall()
            for row in rows:
                if row["kind"] == "publish":
                    message = "发布执行期间服务中断；结果必须人工核验，系统不会自动重发"
                    con.execute(
                        "UPDATE task_queue SET status='manual',last_error=?,lease_until=NULL,updated_at=? WHERE id=?",
                        (message, now, row["id"]),
                    )
                    con.execute(
                        "UPDATE publish_tasks SET status='verification_pending',error=?,updated_at=? "
                        "WHERE id=? AND status IN ('approved','queued','publishing')",
                        (message, now, row["resource_id"]),
                    )
                else:
                    con.execute(
                        "UPDATE task_queue SET status='queued',available_at=?,lease_until=NULL,updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
                    con.execute(
                        "UPDATE search_jobs SET status='pending',error='服务重启后恢复任务' "
                        "WHERE id=? AND status='running'",
                        (row["resource_id"],),
                    )
            con.commit()

    def claim(self, lease_seconds: int = 180) -> QueueItem | None:
        self.recover_expired()
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """SELECT q.* FROM task_queue q
                WHERE q.status IN ('queued','retry_wait') AND q.available_at<=?
                AND (q.account_id IS NULL OR NOT EXISTS(
                    SELECT 1 FROM task_queue active
                    WHERE active.account_id=q.account_id AND active.status='running'))
                ORDER BY q.available_at,q.id LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                con.commit()
                return None
            con.execute(
                """UPDATE task_queue SET status='running',lease_until=?,heartbeat_at=?,
                attempts=attempts+1,updated_at=? WHERE id=?""",
                (lease, now, now, row["id"]),
            )
            con.commit()
            return QueueItem(
                int(row["id"]),
                str(row["kind"]),
                int(row["resource_id"]),
                int(row["account_id"]) if row["account_id"] is not None else None,
                int(row["attempts"]) + 1,
                int(row["max_attempts"]),
            )

    def heartbeat(self, queue_id: int, lease_seconds: int = 180) -> None:
        now_dt = datetime.now(UTC)
        with self._lock, self.db.connect() as con:
            con.execute(
                "UPDATE task_queue SET heartbeat_at=?,lease_until=?,updated_at=? WHERE id=? AND status='running'",
                (
                    now_dt.isoformat(),
                    (now_dt + timedelta(seconds=lease_seconds)).isoformat(),
                    now_dt.isoformat(),
                    queue_id,
                ),
            )
            con.commit()

    def finish(
        self,
        item: QueueItem,
        status: str,
        error: str | None = None,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        now_dt = datetime.now(UTC)
        if retryable and item.attempts < item.max_attempts:
            status = "retry_wait"
            delay = retry_after_seconds or min(300, 5 * (2 ** (item.attempts - 1)))
            available = (now_dt + timedelta(seconds=delay)).isoformat()
        else:
            available = now_dt.isoformat()
            if retryable:
                status = "failed"
        with self._lock, self.db.connect() as con:
            con.execute(
                """UPDATE task_queue SET status=?,available_at=?,lease_until=NULL,heartbeat_at=NULL,
                last_error=?,updated_at=? WHERE id=?""",
                (status, available, error, now_dt.isoformat(), item.id),
            )
            con.commit()

    def cancel(self, kind: str, resource_id: int) -> None:
        with self._lock, self.db.connect() as con:
            con.execute(
                "UPDATE task_queue SET status='cancelled',lease_until=NULL,updated_at=? "
                "WHERE idempotency_key=? AND status IN ('queued','retry_wait')",
                (now_iso(), f"{kind}:{resource_id}"),
            )
            con.commit()

    def acquire_request(self, account_id: int, min_interval: float, daily_limit: int) -> float:
        now_dt = datetime.now(UTC)
        today = date.today().isoformat()
        with self._lock, self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM account_usage WHERE account_id=?", (account_id,)).fetchone()
            if row and row["usage_date"] != today:
                con.execute(
                    "UPDATE account_usage SET usage_date=?,request_count=0,next_request_at=NULL,updated_at=? "
                    "WHERE account_id=?",
                    (today, now_dt.isoformat(), account_id),
                )
                row = con.execute("SELECT * FROM account_usage WHERE account_id=?", (account_id,)).fetchone()
            if not row:
                con.execute(
                    "INSERT INTO account_usage(account_id,usage_date,updated_at) VALUES(?,?,?)",
                    (account_id, today, now_dt.isoformat()),
                )
                row = con.execute("SELECT * FROM account_usage WHERE account_id=?", (account_id,)).fetchone()
            paused = _dt(row["paused_until"])
            if paused and paused > now_dt:
                con.commit()
                raise AccountTemporarilyPaused(row["pause_reason"] or f"账号暂停至 {paused.isoformat()}")
            if int(row["request_count"]) >= daily_limit:
                con.commit()
                raise DailyLimitReached(f"账号今日读取请求已达到上限 {daily_limit}")
            next_at = _dt(row["next_request_at"])
            if next_at and next_at > now_dt:
                con.commit()
                return (next_at - now_dt).total_seconds()
            con.execute(
                """UPDATE account_usage SET request_count=request_count+1,next_request_at=?,
                paused_until=NULL,pause_reason=NULL,updated_at=? WHERE account_id=?""",
                ((now_dt + timedelta(seconds=min_interval)).isoformat(), now_dt.isoformat(), account_id),
            )
            con.commit()
            return 0.0

    def pause_account(self, account_id: int, seconds: int, reason: str) -> None:
        now_dt = datetime.now(UTC)
        until = now_dt + timedelta(seconds=seconds)
        today = date.today().isoformat()
        with self._lock, self.db.connect() as con:
            con.execute(
                """INSERT INTO account_usage(account_id,usage_date,paused_until,pause_reason,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET paused_until=excluded.paused_until,
                pause_reason=excluded.pause_reason,updated_at=excluded.updated_at""",
                (account_id, today, until.isoformat(), reason, now_dt.isoformat()),
            )
            con.commit()

    def queue_counts(self) -> dict[str, int]:
        rows = self.db.fetchall("SELECT status,COUNT(*) count FROM task_queue GROUP BY status")
        return {str(row["status"]): int(row["count"]) for row in rows}
