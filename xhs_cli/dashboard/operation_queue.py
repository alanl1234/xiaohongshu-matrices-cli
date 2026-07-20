"""Independent worker pool for AI and engagement operations."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .operations import OperationItem, OperationsStore


class OperationQueue:
    def __init__(self, store: OperationsStore, ai: Any, engagement: Any, workers: int = 2):
        self.store, self.ai, self.engagement = store, ai, engagement
        self.workers = max(1, workers)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._futures: set[Future[None]] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="xhs-operations")
        self._thread = threading.Thread(target=self._loop, name="xhs-operation-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=False)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._futures = {future for future in self._futures if not future.done()}
            while len(self._futures) < self.workers:
                item = self.store.claim()
                if not item or not self._pool:
                    break
                self._futures.add(self._pool.submit(self._execute, item))
            self._stop.wait(0.5)

    def _execute(self, item: OperationItem) -> None:
        try:
            if item.kind in {"search_brief", "screen_results", "material_research", "agent_draft"}:
                status = self.ai.run(item.resource_id)
                self.store.finish(item, "done" if status == "complete" else "failed")
            elif item.kind == "dm_sync":
                status = self.engagement.sync_thread(item.resource_id)
                self.store.finish(item, "done" if status in {"safe", "human_handoff", "opted_out"} else "failed")
            else:
                status = self.engagement.run(item.resource_id)
                queue_status = (
                    "done" if status == "sent" else ("manual" if status == "verification_pending" else status)
                )
                self.store.finish(item, queue_status)
        except Exception as exc:
            self.store.finish(item, "failed", str(exc))
