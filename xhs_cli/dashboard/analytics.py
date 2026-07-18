"""Post-performance analytics with periodic snapshot collection.

Collects likes / comments / collects / shares snapshots for published notes,
computes engagement rates, and presents per-account & cross-account trends.

Opt-in via XHS_ANALYTICS_ENABLED=1 (default off).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database
from .utils import now_iso

ANALYTICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 note_id TEXT NOT NULL,
 account_id INTEGER,
 title TEXT NOT NULL DEFAULT '',
 likes INTEGER NOT NULL DEFAULT 0,
 comments INTEGER NOT NULL DEFAULT 0,
 collects INTEGER NOT NULL DEFAULT 0,
 shares INTEGER NOT NULL DEFAULT 0,
 engagement_rate REAL NOT NULL DEFAULT 0.0,
 followers_at_time INTEGER,
 snapshot_at TEXT NOT NULL,
 FOREIGN KEY(account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_analytics_snapshots_note ON analytics_snapshots(note_id, snapshot_at);
CREATE INDEX IF NOT EXISTS idx_analytics_snapshots_account ON analytics_snapshots(account_id, snapshot_at);
"""


def analytics_enabled() -> bool:
    return os.getenv("XHS_ANALYTICS_ENABLED", "0").strip() in {"1", "true", "yes"}


class AnalyticsCollector:
    """Periodically snapshots stats for published notes."""

    def __init__(self, db: Database) -> None:
        self.db = db
        with self.db.connect() as con:
            con.executescript(ANALYTICS_SCHEMA)

    def collect(self) -> int:
        """Gather one snapshot for every published note. Returns count of new snapshots."""
        if not analytics_enabled():
            return 0
        published = self.db.fetchall(
            "SELECT id, final_note_id, account_id, title FROM publish_tasks "
            "WHERE status='published' AND final_note_id IS NOT NULL"
        )
        count = 0
        for task in published:
            note = self.db.fetchone("SELECT * FROM notes WHERE note_id=?", (task["final_note_id"],))
            if not note:
                continue
            rate = _engagement_rate(note["likes"], note["comments"], note["collects"], note["shares"])
            self.db.execute(
                "INSERT INTO analytics_snapshots(note_id,account_id,title,likes,comments,collects,shares,"
                "engagement_rate,snapshot_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    note["note_id"],
                    task["account_id"],
                    task["title"],
                    int(note["likes"]),
                    int(note["comments"]),
                    int(note["collects"]),
                    int(note["shares"]),
                    round(rate, 4),
                    now_iso(),
                ),
            )
            count += 1
        return count

    def summary(self, account_id: int | None = None, days: int = 30) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        where = "WHERE snapshot_at >= ?"
        params: list[Any] = [since]
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)

        rows = self.db.fetchall(
            f"SELECT * FROM analytics_snapshots {where} ORDER BY snapshot_at DESC", tuple(params)
        )
        if not rows:
            return {"period_days": days, "snapshot_count": 0, "notes": [], "trend": []}

        # per-note latest stats
        seen: dict[str, dict[str, Any]] = {}
        for r in rows:
            nid = r["note_id"]
            if nid not in seen:
                seen[nid] = dict(r)

        # aggregate trend by day
        daily: dict[str, dict[str, float]] = {}
        for r in rows:
            day = r["snapshot_at"][:10]
            if day not in daily:
                daily[day] = {"likes": 0, "comments": 0, "collects": 0, "shares": 0, "count": 0}
            d = daily[day]
            d["likes"] += r["likes"]
            d["comments"] += r["comments"]
            d["collects"] += r["collects"]
            d["shares"] += r["shares"]
            d["count"] += 1

        trend = [
            {"date": day, "avg_likes": round(v["likes"] / max(v["count"], 1), 1), "note_count": v["count"]}
            for day, v in sorted(daily.items())
        ]

        return {
            "period_days": days,
            "snapshot_count": len(rows),
            "notes": sorted(seen.values(), key=lambda x: x.get("engagement_rate", 0), reverse=True),
            "trend": trend,
        }

    def account_ranking(self, days: int = 30) -> list[dict[str, Any]]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.db.fetchall(
            "SELECT a.alias, a.id, COUNT(s.id) as snapshots, AVG(s.engagement_rate) as avg_er "
            "FROM analytics_snapshots s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.snapshot_at >= ? GROUP BY s.account_id ORDER BY avg_er DESC",
            (since,),
        )
        return [dict(r) for r in rows]


def _engagement_rate(likes: int, comments: int, collects: int, shares: int, scale: float = 1.0) -> float:
    """Simple weighted engagement score: 1×likes + 2×comments + 3×collects + 4×shares."""
    weighted = likes * 1 + comments * 2 + collects * 3 + shares * 4
    return weighted / max(scale, 1)
