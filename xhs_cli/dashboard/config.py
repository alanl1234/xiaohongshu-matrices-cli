"""Dashboard paths and runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DashboardConfig:
    data_dir: Path
    database_path: Path
    library_dir: Path
    profiles_dir: Path
    uploads_dir: Path
    screenshots_dir: Path
    publish_cooldown_seconds: int = 600
    max_accounts: int = 10
    worker_threads: int = 2
    request_interval_seconds: float = 1.0
    daily_request_limit: int = 2500
    queue_lease_seconds: int = 180
    queue_poll_seconds: float = 0.5

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> DashboardConfig:
        root = Path(data_dir or os.getenv("XHS_DASHBOARD_DATA") or (Path.home() / ".xiaohongshu-cli" / "dashboard"))
        root = root.expanduser().resolve()
        config = cls(
            root,
            root / "dashboard.sqlite3",
            root / "library",
            root / "profiles",
            root / "uploads",
            root / "screenshots",
            publish_cooldown_seconds=max(0, int(os.getenv("XHS_PUBLISH_COOLDOWN", "600"))),
            worker_threads=max(1, int(os.getenv("XHS_WORKERS", "2"))),
            request_interval_seconds=max(0.2, float(os.getenv("XHS_REQUEST_INTERVAL", "1.0"))),
            daily_request_limit=max(1, int(os.getenv("XHS_DAILY_REQUEST_LIMIT", "2500"))),
            queue_lease_seconds=max(60, int(os.getenv("XHS_QUEUE_LEASE", "180"))),
            queue_poll_seconds=max(0.1, float(os.getenv("XHS_QUEUE_POLL", "0.5"))),
        )
        config.ensure_directories()
        return config

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.library_dir, self.profiles_dir, self.uploads_dir, self.screenshots_dir):
            path.mkdir(parents=True, exist_ok=True)
