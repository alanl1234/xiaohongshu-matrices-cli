"""SQLite-backed task dispatcher with leases, heartbeat, and safe recovery."""

from __future__ import annotations

import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .db import Database
from .persistence import P0Store, QueueItem


@dataclass(frozen=True)
class QueueOutcome:
    status: str
    error: str | None = None
    retryable: bool = False
    retry_after_seconds: int | None = None


class DurableTaskQueue:
    def __init__(
        self,
        db: Database,
        store: P0Store,
        collector: Any,
        publisher: Any,
        *,
        workers: int = 2,
        lease_seconds: int = 180,
        poll_seconds: float = 0.5,
    ):
        self.db = db
        self.store = store
        self.collector = collector
        self.publisher = publisher
        self.workers = max(1, workers)
        self.lease_seconds = max(60, lease_seconds)
        self.poll_seconds = max(0.1, poll_seconds)
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._futures: set[Future[None]] = set()

    def start(self) -> None:
        if self._supervisor and self._supervisor.is_alive():
            return
        self.store.recover_expired()
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="xhs-durable")
        self._supervisor = threading.Thread(target=self._loop, name="xhs-queue-supervisor", daemon=True)
        self._supervisor.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._supervisor:
            self._supervisor.join(timeout=timeout)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=False)

    def enqueue_search(self, job_id: int, account_id: int) -> int:
        return self.store.enqueue("search", job_id, account_id, max_attempts=3)

    def enqueue_publish(self, task_id: int, account_id: int) -> int:
        return self.store.enqueue("publish", task_id, account_id, max_attempts=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._futures = {future for future in self._futures if not future.done()}
            while len(self._futures) < self.workers and not self._stop.is_set():
                item = self.store.claim(self.lease_seconds)
                if not item:
                    break
                if not self._pool:
                    return
                self._futures.add(self._pool.submit(self._execute, item))
            self._stop.wait(self.poll_seconds)

    def _heartbeat(self, item: QueueItem, stopped: threading.Event) -> None:
        interval = max(10.0, self.lease_seconds / 3)
        while not stopped.wait(interval):
            self.store.heartbeat(item.id, self.lease_seconds)

    def _execute(self, item: QueueItem) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(item, heartbeat_stop), daemon=True)
        heartbeat.start()
        try:
            outcome = self._dispatch(item)
            self.store.finish(
                item,
                outcome.status,
                outcome.error,
                retryable=outcome.retryable,
                retry_after_seconds=outcome.retry_after_seconds,
            )
        except Exception as exc:
            self.store.finish(item, "failed", str(exc), retryable=item.kind == "search")
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)

    def _dispatch(self, item: QueueItem) -> QueueOutcome:
        if item.kind == "search":
            status = self.collector.run(item.resource_id)
            job = self.db.fetchone("SELECT status,error FROM search_jobs WHERE id=?", (item.resource_id,)) or {}
            status = status or job.get("status") or "failed"
            error = job.get("error")
            if status == "complete":
                return QueueOutcome("done")
            if status in {"paused", "cancelled"}:
                return QueueOutcome("manual" if status == "paused" else "cancelled", error)
            return QueueOutcome("failed", error, retryable=True)

        status = self.publisher.run(item.resource_id)
        task = self.db.fetchone("SELECT status,error FROM publish_tasks WHERE id=?", (item.resource_id,)) or {}
        status = status or task.get("status") or "failed"
        error = task.get("error")
        if status == "published":
            return QueueOutcome("done")
        if status == "verification_pending":
            return QueueOutcome("manual", error)
        if status == "cancelled":
            return QueueOutcome("cancelled")
        attempt = (
            self.db.fetchone(
                "SELECT stage,error_category FROM publish_attempts WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (item.resource_id,),
            )
            or {}
        )
        safe_stage = attempt.get("stage") in {"starting", "opening_creator", "uploading_images", "filling_content"}
        category = attempt.get("error_category")
        retryable = safe_stage and category in {"transient", "cooldown"}
        retry_after = None
        if retryable:
            self.db.update("publish_tasks", item.resource_id, status="queued")
        if category == "cooldown" and error:
            match = re.search(r"(\d+) 秒", error)
            retry_after = int(match.group(1)) + 2 if match else 60
        return QueueOutcome("failed", error, retryable=retryable, retry_after_seconds=retry_after)
