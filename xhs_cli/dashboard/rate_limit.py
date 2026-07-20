"""Persisted, per-account request pacing for reverse read APIs."""

from __future__ import annotations

import time

from .persistence import P0Store


class AccountRateLimiter:
    def __init__(self, store: P0Store, *, interval_seconds: float = 1.0, daily_limit: int = 2500):
        self.store = store
        self.interval_seconds = max(0.2, interval_seconds)
        self.daily_limit = max(1, daily_limit)

    def acquire(self, account_id: int) -> None:
        while True:
            wait = self.store.acquire_request(account_id, self.interval_seconds, self.daily_limit)
            if wait <= 0:
                return
            time.sleep(min(wait, 2.0))

    def pause(self, account_id: int, reason: str, seconds: int = 1800) -> None:
        self.store.pause_account(account_id, seconds, reason)
