"""SQLite repository for the single-machine dashboard."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .utils import json_dumps, now_iso

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS accounts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, alias TEXT NOT NULL UNIQUE,
 xhs_user_id TEXT NOT NULL DEFAULT '', nickname TEXT NOT NULL DEFAULT '',
 profile_dir TEXT NOT NULL UNIQUE, login_status TEXT NOT NULL DEFAULT 'unbound',
 last_verified_at TEXT, enabled INTEGER NOT NULL DEFAULT 1, last_publish_at TEXT,
 last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, account_id INTEGER,
 keywords_json TEXT NOT NULL DEFAULT '[]', topics_json TEXT NOT NULL DEFAULT '[]',
 author_ids_json TEXT NOT NULL DEFAULT '[]', start_date TEXT, end_date TEXT,
 media_type TEXT NOT NULL DEFAULT 'all', include_comments INTEGER NOT NULL DEFAULT 1,
 comment_limit INTEGER NOT NULL DEFAULT 100, max_pages INTEGER NOT NULL DEFAULT 3,
 min_score INTEGER NOT NULL DEFAULT 1000, min_likes INTEGER NOT NULL DEFAULT 0,
 min_collects INTEGER NOT NULL DEFAULT 0, min_comments INTEGER NOT NULL DEFAULT 0,
 weights_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0,
 result_count INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL,
 started_at TEXT, finished_at TEXT, FOREIGN KEY(account_id) REFERENCES accounts(id));
CREATE TABLE IF NOT EXISTS notes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, note_id TEXT NOT NULL UNIQUE,
 author_id TEXT NOT NULL DEFAULT '', author_name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
 body TEXT NOT NULL DEFAULT '', published_at TEXT, media_type TEXT NOT NULL DEFAULT 'image',
 original_url TEXT NOT NULL DEFAULT '', likes INTEGER NOT NULL DEFAULT 0,
 collects INTEGER NOT NULL DEFAULT 0, comments INTEGER NOT NULL DEFAULT 0,
 shares INTEGER NOT NULL DEFAULT 0, viral_score INTEGER NOT NULL DEFAULT 0,
 topics_json TEXT NOT NULL DEFAULT '[]', images_json TEXT NOT NULL DEFAULT '[]',
 comments_json TEXT NOT NULL DEFAULT '[]', xsec_token TEXT NOT NULL DEFAULT '',
 xsec_source TEXT NOT NULL DEFAULT 'pc_search', raw_json TEXT NOT NULL DEFAULT '{}',
 collected_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_job_notes (
 job_id INTEGER NOT NULL, note_id INTEGER NOT NULL, PRIMARY KEY(job_id,note_id),
 FOREIGN KEY(job_id) REFERENCES search_jobs(id) ON DELETE CASCADE,
 FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS export_bundles (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, note_id INTEGER NOT NULL,
 directory TEXT NOT NULL, status TEXT NOT NULL, checksum TEXT NOT NULL,
 image_results_json TEXT NOT NULL DEFAULT '[]', error TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, UNIQUE(job_id,note_id),
 FOREIGN KEY(job_id) REFERENCES search_jobs(id) ON DELETE CASCADE,
 FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS publish_tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, title TEXT NOT NULL,
 body TEXT NOT NULL, topics_json TEXT NOT NULL DEFAULT '[]', images_json TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'pending_review', approved_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 final_note_id TEXT, final_url TEXT, source_dir TEXT, content_fingerprint TEXT, error TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, FOREIGN KEY(account_id) REFERENCES accounts(id));
CREATE TABLE IF NOT EXISTS publish_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'starting', message TEXT,
 screenshot_path TEXT, final_note_id TEXT, final_url TEXT,
 FOREIGN KEY(task_id) REFERENCES publish_tasks(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_search_jobs_status ON search_jobs(status);
CREATE INDEX IF NOT EXISTS idx_publish_tasks_status ON publish_tasks(status);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def initialize(self) -> None:
        with self.connect() as con:
            con.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock, self.connect() as con:
            cursor = con.execute(sql, params)
            con.commit()
            return int(cursor.lastrowid)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(sql, params).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as con:
            return [dict(row) for row in con.execute(sql, params).fetchall()]

    def create_account(self, alias: str, profile_dir: str) -> int:
        now = now_iso()
        return self.execute(
            "INSERT INTO accounts(alias,profile_dir,created_at,updated_at) VALUES(?,?,?,?)",
            (alias, profile_dir, now, now),
        )

    def update(self, table: str, row_id: int, **values: Any) -> None:
        allowed = {"accounts", "search_jobs", "publish_tasks", "publish_attempts"}
        if table not in allowed or not values:
            return
        if table in {"accounts", "publish_tasks"}:
            values["updated_at"] = now_iso()
        assignments = ",".join(f"{key}=?" for key in values)
        self.execute(f"UPDATE {table} SET {assignments} WHERE id=?", (*values.values(), row_id))

    def create_search_job(self, values: dict[str, Any]) -> int:
        defaults = {
            "account_id": None,
            "keywords_json": "[]",
            "topics_json": "[]",
            "author_ids_json": "[]",
            "start_date": None,
            "end_date": None,
            "media_type": "all",
            "include_comments": 1,
            "comment_limit": 100,
            "max_pages": 3,
            "min_score": 1000,
            "min_likes": 0,
            "min_collects": 0,
            "min_comments": 0,
            "weights_json": json_dumps({"likes": 1, "collects": 2, "comments": 3, "shares": 1}),
        }
        defaults.update(values)
        fields = [
            "name",
            "account_id",
            "keywords_json",
            "topics_json",
            "author_ids_json",
            "start_date",
            "end_date",
            "media_type",
            "include_comments",
            "comment_limit",
            "max_pages",
            "min_score",
            "min_likes",
            "min_collects",
            "min_comments",
            "weights_json",
        ]
        marks = ",".join("?" for _ in fields)
        return self.execute(
            f"INSERT INTO search_jobs({','.join(fields)},created_at) VALUES({marks},?)",
            tuple(defaults[field] for field in fields) + (now_iso(),),
        )

    def upsert_note(self, note: dict[str, Any]) -> int:
        existing = self.fetchone("SELECT id FROM notes WHERE note_id=?", (note["note_id"],))
        fields = [
            "author_id",
            "author_name",
            "title",
            "body",
            "published_at",
            "media_type",
            "original_url",
            "likes",
            "collects",
            "comments",
            "shares",
            "viral_score",
            "topics_json",
            "images_json",
            "comments_json",
            "xsec_token",
            "xsec_source",
            "raw_json",
        ]
        values = tuple(note.get(field) for field in fields)
        if existing:
            self.execute(
                f"UPDATE notes SET {','.join(f'{field}=?' for field in fields)},updated_at=? WHERE id=?",
                (*values, now_iso(), existing["id"]),
            )
            return int(existing["id"])
        marks = ",".join("?" for _ in fields)
        now = now_iso()
        return self.execute(
            f"INSERT INTO notes(note_id,{','.join(fields)},collected_at,updated_at) VALUES(?,{marks},?,?)",
            (note["note_id"], *values, now, now),
        )

    def link_job_note(self, job_id: int, note_id: int) -> None:
        self.execute("INSERT OR IGNORE INTO search_job_notes(job_id,note_id) VALUES(?,?)", (job_id, note_id))

    def upsert_export(
        self,
        job_id: int,
        note_id: int,
        directory: str,
        status: str,
        checksum: str,
        image_results: str,
        error: str | None = None,
    ) -> None:
        now = now_iso()
        self.execute(
            """INSERT INTO export_bundles(
        job_id,note_id,directory,status,checksum,image_results_json,error,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id,note_id) DO UPDATE SET directory=excluded.directory,
        status=excluded.status,checksum=excluded.checksum,image_results_json=excluded.image_results_json,
        error=excluded.error,updated_at=excluded.updated_at""",
            (job_id, note_id, directory, status, checksum, image_results, error, now, now),
        )

    def create_publish_task(
        self, account_id: int, title: str, body: str, topics_json: str, images_json: str, source_dir: str | None = None
    ) -> int:
        now = now_iso()
        return self.execute(
            """INSERT INTO publish_tasks(account_id,title,body,topics_json,images_json,source_dir,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)""",
            (account_id, title, body, topics_json, images_json, source_dir, now, now),
        )

    def create_attempt(self, task_id: int) -> int:
        return self.execute(
            "INSERT INTO publish_attempts(task_id,started_at,status) VALUES(?,?,?)", (task_id, now_iso(), "running")
        )
