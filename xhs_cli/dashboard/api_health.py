"""Lightweight API health probe for XiaoHongShu endpoints.

Periodically tests key API endpoints to detect breaking changes.
Runs as part of the orchestrator tick (opt-in). Results are stored
in DB and surfaced in the Dashboard.

Opt-in via XHS_API_HEALTH_ENABLED=1 (default off).
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from .db import Database
from .utils import now_iso

HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_health_checks (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 endpoint TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'unknown',
 latency_ms REAL,
 status_code INTEGER,
 error_message TEXT,
 checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_health_endpoint ON api_health_checks(endpoint, checked_at);
"""

ENDPOINTS: list[tuple[str, str, dict[str, Any]]] = [
    ("search", "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes", {"timeout": 10}),
    ("homefeed", "https://edith.xiaohongshu.com/api/sns/web/v1/homefeed", {"timeout": 10}),
    ("note_detail", "https://edith.xiaohongshu.com/api/sns/web/v1/feed", {"timeout": 10}),
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


def health_enabled() -> bool:
    return os.getenv("XHS_API_HEALTH_ENABLED", "0").strip() in {"1", "true", "yes"}


class ApiHealthMonitor:
    def __init__(self, db: Database) -> None:
        self.db = db
        with self.db.connect() as con:
            con.executescript(HEALTH_SCHEMA)

    def probe(self) -> dict[str, str]:
        """Probe all tracked endpoints, record results, return summary."""
        if not health_enabled():
            return {}
        results: dict[str, str] = {}
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
        }
        for name, url, opts in ENDPOINTS:
            start = time.monotonic()
            try:
                resp = httpx.get(url, headers=headers, timeout=opts.get("timeout", 10))
                status = "healthy" if resp.status_code < 500 else "degraded"
                latency = round((time.monotonic() - start) * 1000, 1)
                sc = resp.status_code
                err = ""
            except httpx.TimeoutException:
                status, latency, sc, err = "timeout", round((time.monotonic() - start) * 1000, 1), 0, "timeout"
            except Exception as exc:
                status, latency, sc, err = "unreachable", 0.0, 0, str(exc)[:200]
            self.db.execute(
                "INSERT INTO api_health_checks(endpoint,status,latency_ms,status_code,error_message,checked_at) "
                "VALUES(?,?,?,?,?,?)",
                (name, status, latency, sc, err, now_iso()),
            )
            results[name] = status
        return results

    def latest(self) -> list[dict[str, Any]]:
        """Return the most recent result per endpoint."""
        rows = self.db.fetchall(
            "SELECT * FROM api_health_checks WHERE id IN "
            "(SELECT MAX(id) FROM api_health_checks GROUP BY endpoint) "
            "ORDER BY endpoint"
        )
        return [dict(r) for r in rows]

    def history(self, endpoint: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM api_health_checks WHERE endpoint=? ORDER BY id DESC LIMIT ?",
            (endpoint, limit),
        )
        return [dict(r) for r in rows]
