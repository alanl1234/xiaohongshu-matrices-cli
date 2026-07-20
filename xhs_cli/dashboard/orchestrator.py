"""受治理的全自动编排层。

本模块在**不改动任何现有手动流程与数据表**的前提下，补上项目缺失的"编排 / 串联 / 排期"
能力。它驱动已有的两套队列（DurableTaskQueue 跑 search/publish，OperationQueue 跑
material_research/agent_draft/互动），并一律经由项目的治理引擎（限流、opt-out、敏感词、
相似度）把关。

所有"全自动"行为都是 **opt-in**，默认完全不启用：

- XHS_ORCHESTRATOR=1                 启动编排常驻线程（目标调度 + 流水线串联 + 自动发布 + 自动执行已批准任务）
- XHS_AUTO_PUBLISH=1                 把 AI 草稿自动转成待审核发布任务（仍需人工批准）
- XHS_AUTO_PUBLISH=approve          在治理判定通过后自动批准并排期发布（无人值守发布）
- XHS_ASSET_POOL_DIR=<dir>           自动发布配图目录（发布必须 1–18 张图，AI 草稿只有文字）
- XHS_ENGAGEMENT_MODE                shadow / inbound / reviewed —— 复用既有互动灰度，决定哪些已批准任务自动执行
- XHS_DAILY_PUBLISH_LIMIT=5          每账号每日最多自动发布数（默认 5）
- XHS_ORCHESTRATOR_TICK=60          编排线程轮询间隔（秒）

记账用一张自建表 orch_markers（CREATE TABLE IF NOT EXISTS），不触碰项目既有迁移。
"""

from __future__ import annotations

import json
import os
import random
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI

from .config import DashboardConfig
from .db import Database
from .governance import evaluate_content
from .operations import OperationsStore
from .utils import json_dumps, json_loads, now_iso, split_terms


class Orchestrator:
    def __init__(
        self,
        db: Database,
        config: DashboardConfig,
        p0_store: Any,
        p0_queue: Any,
        ops_store: OperationsStore,
        engagement: Any,
        ai: Any,
    ) -> None:
        self.db = db
        self.config = config
        self.p0_store = p0_store
        self.p0_queue = p0_queue
        self.ops_store = ops_store
        self.engagement = engagement
        self.ai = ai

        self.enabled = os.getenv("XHS_ORCHESTRATOR") == "1"
        self.auto_publish_mode = os.getenv("XHS_AUTO_PUBLISH", "")
        self.auto_approve = False  # Agents and the orchestrator can never replace human approval
        self.mode = os.getenv("XHS_ENGAGEMENT_MODE", "shadow").strip().lower()
        self.tick_seconds = max(10, int(os.getenv("XHS_ORCHESTRATOR_TICK", "60")))
        self.daily_publish_limit = max(1, int(os.getenv("XHS_DAILY_PUBLISH_LIMIT", "5")))
        pool = os.getenv("XHS_ASSET_POOL_DIR", "")
        self.asset_pool = Path(pool).expanduser().resolve() if pool else None
        self.goals_path = config.data_dir / "orchestrator_goals.json"

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ensure_schema()

    # ── 记账 ───────────────────────────────────────────────────────────────
    def _ensure_schema(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS orch_markers("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
        )

    def _mark(self, key: str) -> None:
        self._mark_at(key, "1")

    def _mark_at(self, key: str, value: str) -> None:
        if self.db.fetchone("SELECT key FROM orch_markers WHERE key=?", (key,)):
            self.db.execute("UPDATE orch_markers SET value=?,updated_at=? WHERE key=?", (value, now_iso(), key))
        else:
            self.db.execute("INSERT INTO orch_markers(key,value,updated_at) VALUES(?,?,?)", (key, value, now_iso()))

    def _marked(self, key: str) -> bool:
        return bool(self.db.fetchone("SELECT key FROM orch_markers WHERE key=?", (key,)))

    def _marker_value(self, key: str) -> str:
        row = self.db.fetchone("SELECT value FROM orch_markers WHERE key=?", (key,))
        return row["value"] if row else ""

    # ── 生命周期 ───────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xhs-orchestrator", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # 单轮异常不应杀死常驻线程
                self.db.execute(
                    "INSERT INTO orch_markers(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (f"last_error:{now_iso()}", str(exc)[:500], now_iso()),
                )
            self._stop.wait(self.tick_seconds)

    def _tick(self) -> None:
        goals = self._load_goals()
        self._dispatch_goals(goals)
        self._advance_pipeline()
        if self.auto_publish_mode:
            self._auto_publish()
            self._promote_scheduled_publishes()
        self._auto_engage()
        self._tick_analytics()
        self._tick_health()

    def _tick_analytics(self) -> None:
        from .analytics import AnalyticsCollector, analytics_enabled
        if analytics_enabled():
            try:
                AnalyticsCollector(self.db).collect()
            except Exception:
                pass

    def _tick_health(self) -> None:
        from .api_health import ApiHealthMonitor, health_enabled
        if health_enabled():
            try:
                ApiHealthMonitor(self.db).probe()
            except Exception:
                pass

    # ── 目标调度 ───────────────────────────────────────────────────────────
    def _load_goals(self) -> list[dict[str, Any]]:
        if not self.goals_path.is_file():
            return []
        try:
            goals = json.loads(self.goals_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(goals, list):
            return []
        cleaned = []
        for goal in goals:
            if not isinstance(goal, dict) or not goal.get("id") or not goal.get("objective"):
                continue
            goal.setdefault("enabled", True)
            goal.setdefault("cadence_hours", 24)
            goal.setdefault("max_candidates", 8)
            goal.setdefault("account_id", None)
            goal.setdefault("persona_id", None)
            cleaned.append(goal)
        return cleaned

    def _dispatch_goals(self, goals: list[dict[str, Any]]) -> None:
        for goal in goals:
            if not goal.get("enabled", True):
                continue
            key = f"goal_last:{goal['id']}"
            marker = self.db.fetchone("SELECT updated_at FROM orch_markers WHERE key=?", (key,))
            if marker:
                last = datetime.fromisoformat(marker["updated_at"])
                if datetime.now(UTC) - last < timedelta(hours=float(goal.get("cadence_hours", 24))):
                    continue
            self._enqueue_ai_run(
                "search_brief",
                {
                    "objective": goal["objective"],
                    "target_audience": goal.get("target_audience", ""),
                    "max_candidates": int(goal.get("max_candidates", 8)),
                },
            )
            self._mark(key)

    # ── 流水线串联 ─────────────────────────────────────────────────────────
    def _enqueue_ai_run(self, kind: str, payload: dict[str, Any]) -> int:
        run_id = self.ops_store.create_agent_run(kind, payload)
        self.ops_store.enqueue(kind, run_id, None)
        return run_id

    def _ready_account(self, account_id: int | None = None) -> dict[str, Any] | None:
        if account_id:
            account = self.db.fetchone(
                "SELECT * FROM accounts WHERE id=? AND enabled=1 AND login_status='ready'", (account_id,)
            )
            if account:
                return account
        return self.db.fetchone("SELECT * FROM accounts WHERE enabled=1 AND login_status='ready' ORDER BY id LIMIT 1")

    def create_search_jobs_from_plan(
        self,
        plan: Any,
        objective: str,
        target_audience: str,
        max_candidates: int,
        account_id: int | None = None,
    ) -> list[int]:
        """Turn a decomposed SearchPlan into one search job per sub-task.

        Returns the created job ids. Each job carries the sub-task's own terms,
        dates, media type and floor, plus a context marker (angle + criteria +
        quota) the pipeline uses later for LLM-based screening.
        """
        from .ai import SearchPlan

        if not isinstance(plan, SearchPlan):
            plan = SearchPlan.model_validate(plan)
        account = self._ready_account(account_id) if account_id else self._ready_account()
        if not account:
            return []
        created: list[int] = []
        for sub in plan.subtasks:
            job_id = self.db.create_search_job(
                {
                    "name": f"{plan.name} · {sub.angle}",
                    "account_id": account["id"],
                    "keywords_json": json_dumps(sub.keywords),
                    "topics_json": json_dumps(sub.topics),
                    "author_ids_json": json_dumps(sub.author_ids),
                    "start_date": sub.start_date,
                    "end_date": sub.end_date,
                    "media_type": sub.media_type,
                    "min_score": max(0, int(sub.min_score)),
                }
            )
            self.p0_queue.enqueue_search(job_id, account["id"])
            self._mark(f"orch_job:{job_id}")
            self._mark_at(
                f"job_ctx:{job_id}",
                json_dumps(
                    {
                        "objective": objective,
                        "target_audience": target_audience,
                        "angle": sub.angle,
                        "criteria": sub.criteria,
                        "priority": sub.priority,
                        "max_candidates": max_candidates,
                    }
                ),
            )
            created.append(job_id)
        return created

    def _advance_pipeline(self) -> None:
        # 1) search_brief 完成 -> 每个检索子任务创建一个采集任务
        for run in self.db.fetchall("SELECT * FROM agent_runs WHERE kind='search_brief' AND status='complete'"):
            if self._marked(f"brief_done:{run['id']}"):
                continue
            plan = json_loads(run["output_json"], {})
            payload = json_loads(run["input_json"], {})
            max_candidates = int(payload.get("max_candidates") or 8)
            objective = str(payload.get("objective", ""))
            target_audience = str(payload.get("target_audience", ""))
            self.create_search_jobs_from_plan(plan, objective, target_audience, max_candidates)
            self._mark(f"brief_done:{run['id']}")

        # 2) 编排创建的采集任务完成 -> 按角度筛选标准做 LLM 相关性筛选（非热度前 N）
        for job in self.db.fetchall("SELECT * FROM search_jobs WHERE status='complete'"):
            if self._marked(f"job_screen:{job['id']}"):
                continue
            if not self._marked(f"orch_job:{job['id']}"):
                continue
            ctx = json_loads(self._marker_value(f"job_ctx:{job['id']}") or "{}", {})
            notes = self.db.fetchall(
                "SELECT n.* FROM notes n JOIN search_job_notes jn ON jn.note_id=n.id WHERE jn.job_id=?",
                (job["id"],),
            )
            self._mark(f"job_screen:{job['id']}")
            if not notes:
                continue
            candidates = [
                {
                    "note_id": n["note_id"],
                    "title": n["title"],
                    "body": n["body"],
                    "topics": json_loads(n["topics_json"], []),
                    "comments": json_loads(n["comments_json"], [])[:5],
                }
                for n in notes
            ]
            run_id = self._enqueue_ai_run(
                "screen_results",
                {
                    "objective": ctx.get("objective", ""),
                    "target_audience": ctx.get("target_audience", ""),
                    "angle": ctx.get("angle", ""),
                    "criteria": ctx.get("criteria", ""),
                    "candidates": candidates,
                },
            )
            self._mark_at(
                f"screen_ctx:{run_id}",
                json_dumps(
                    {
                        "job_id": job["id"],
                        "objective": ctx.get("objective", ""),
                        "target_audience": ctx.get("target_audience", ""),
                        "max_candidates": ctx.get("max_candidates", 8),
                    }
                ),
            )

        # 3) 筛选完成 -> 仅对选中笔记做素材研究
        for run in self.db.fetchall("SELECT * FROM agent_runs WHERE kind='screen_results' AND status='complete'"):
            if self._marked(f"screen_material:{run['id']}"):
                continue
            report = json_loads(run["output_json"], {})
            selected_ids = [s.get("note_id") for s in report.get("selections", []) if s.get("selected")]
            ctx = json_loads(self._marker_value(f"screen_ctx:{run['id']}") or "{}", {})
            self._mark(f"screen_material:{run['id']}")
            if not selected_ids:
                continue
            placeholders = ",".join("?" for _ in selected_ids)
            notes = self.db.fetchall(
                f"SELECT note_id,title,body,topics_json,comments_json FROM notes "
                f"WHERE note_id IN ({placeholders})",
                selected_ids,
            )
            if not notes:
                continue
            candidates = [
                {
                    "note_id": n["note_id"],
                    "title": n["title"],
                    "body": n["body"],
                    "topics": json_loads(n["topics_json"], []),
                    "comments": json_loads(n["comments_json"], [])[:5],
                }
                for n in notes
            ]
            run_id = self._enqueue_ai_run(
                "material_research",
                {"objective": ctx.get("objective", ""), "candidates": candidates},
            )
            self._mark_at(
                f"research_ctx:{run_id}",
                json_dumps(
                    {
                        "max_candidates": ctx.get("max_candidates", 8),
                        "objective": ctx.get("objective", ""),
                    }
                ),
            )

        # 4) 素材研究完成 -> 内容草稿（按相关性排序，受 max_candidates 配额控制，不再硬编码前 3）
        for run in self.db.fetchall("SELECT * FROM agent_runs WHERE kind='material_research' AND status='complete'"):
            if self._marked(f"research_draft:{run['id']}"):
                continue
            if not self._marked(f"research_ctx:{run['id']}"):
                continue
            report = json_loads(run["output_json"], {})
            ctx = json_loads(self._marker_value(f"research_ctx:{run['id']}") or "{}", {})
            account = self._ready_account()
            self._mark(f"research_draft:{run['id']}")
            if not account:
                continue
            insights = sorted(
                report.get("candidates", []),
                key=lambda c: float(c.get("relevance_score", 0) or 0),
                reverse=True,
            )
            cap = int(ctx.get("max_candidates") or len(insights))
            for insight in insights[:cap]:
                payload = {
                    "kind": "publish",
                    "account_id": account["id"],
                    "persona_id": None,
                    "objective": insight.get("derivative_angles"),
                    "source_ids": [str(insight.get("note_id", ""))],
                }
                self._enqueue_ai_run("agent_draft", payload)

    # ── 受治理自动发布 ─────────────────────────────────────────────────────
    @staticmethod
    def _publish_gate(context: dict[str, Any]) -> tuple[list[str], str] | None:
        gate = context.get("publish_gate")
        if not isinstance(gate, dict):
            return None
        if gate.get("source_status") not in {"owned", "authorized"}:
            return None
        if not gate.get("derivative_completed") or not gate.get("rights_evidence"):
            return None
        refs = gate.get("source_refs")
        images = gate.get("images")
        if not isinstance(refs, list) or not refs or not isinstance(images, list) or not 1 <= len(images) <= 18:
            return None
        resolved: list[str] = []
        for raw in images:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                return None
            path = path.resolve()
            if not path.is_file():
                return None
            resolved.append(str(path))
        return resolved, str(gate.get("final_asset_dir") or "")

    def _auto_publish(self) -> None:
        """Convert gate-complete Agent drafts into pending-review publish tasks."""
        for draft in self.db.fetchall("SELECT * FROM drafts WHERE kind='publish' AND status='pending_review'"):
            did = draft["id"]
            if self._marked(f"draft_pub:{did}"):
                continue
            account_id = draft.get("account_id")
            if not account_id:
                continue
            account = self.db.fetchone("SELECT * FROM accounts WHERE id=? AND enabled=1", (account_id,))
            if not account:
                continue
            content = draft["content"] or ""
            context = json_loads(draft.get("context_json") or "{}", {})
            policy = evaluate_content(content)
            if policy.decision == "block" or context.get("risk") == "block":
                continue
            gate = self._publish_gate(context)
            if gate is None:
                continue  # The Agent may complete the gate later, so leave this retryable
            images, source_dir = gate
            title = (draft.get("title") or "")[:20] or content[:20]
            self.db.create_publish_task(
                account_id,
                title,
                content[:1000],
                json_dumps(split_terms(context.get("topics", []))),
                json_dumps(images),
                source_dir or None,
            )
            self._mark(f"draft_pub:{did}")

    def _next_publish_slot(self, account_id: int) -> datetime:
        jitter = random.uniform(0, 300)
        base = datetime.now(UTC) + timedelta(seconds=self.config.publish_cooldown_seconds + jitter)
        return base

    def _published_today(self, account_id: int) -> int:
        row = self.db.fetchone(
            "SELECT COUNT(*) n FROM publish_tasks WHERE account_id=? AND status='published' "
            "AND date(created_at)=date('now')",
            (account_id,),
        )
        return int((row or {}).get("n", 0))

    def _promote_scheduled_publishes(self) -> None:
        for task in self.db.fetchall("SELECT * FROM publish_tasks WHERE status='approved'"):
            marker = self.db.fetchone("SELECT value FROM orch_markers WHERE key=?", (f"pub_at:{task['id']}",))
            if not marker:
                continue
            slot = datetime.fromisoformat(marker["value"])
            if datetime.now(UTC) < slot:
                continue
            account = self.db.fetchone("SELECT * FROM accounts WHERE id=?", (task["account_id"],))
            if account and account.get("last_publish_at"):
                last = datetime.fromisoformat(account["last_publish_at"])
                if datetime.now(UTC) - last < timedelta(seconds=self.config.publish_cooldown_seconds):
                    continue
            if self._published_today(task["account_id"]) >= self.daily_publish_limit:
                self._mark_at(f"pub_at:{task['id']}", (datetime.now(UTC) + timedelta(days=1)).isoformat())
                continue
            self.db.update("publish_tasks", task["id"], status="queued")
            self.p0_queue.enqueue_publish(task["id"], task["account_id"])

    # ── 自动执行已批准互动（受治理引擎把关）─────────────────────────────────
    def _auto_engage(self) -> None:
        if self.mode == "shadow":
            return

        # 入站会话定期同步（命中敏感/opt-out 由 GovernanceService 自动转人工）
        if os.getenv("XHS_AUTO_ENGAGE") == "1":
            for thread in self.db.fetchall("SELECT * FROM engagement_threads WHERE status='active' AND warm_lead=1"):
                key = f"sync:{thread['id']}"
                last = self.db.fetchone("SELECT updated_at FROM orch_markers WHERE key=?", (key,))
                if last and datetime.now(UTC) - datetime.fromisoformat(last["updated_at"]) < timedelta(hours=6):
                    continue
                self.ops_store.enqueue("dm_sync", thread["id"], int(thread["account_id"]))
                self._mark(key)

        # 已批准任务 -> 排队执行（再次经治理引擎 preflight）
        for task in self.db.fetchall("SELECT * FROM engagement_tasks WHERE status='approved'"):
            if self._marked(f"eng_run:{task['id']}"):
                continue
            self._mark(f"eng_run:{task['id']}")
            if self.mode == "inbound" and task["kind"] in {"comment", "dm_outbound"}:
                continue
            try:
                self.engagement.governance.preflight(task)
            except Exception:
                continue  # 被治理引擎拦截，留给人工
            self.db.execute(
                "UPDATE engagement_tasks SET status='queued',updated_at=datetime('now') WHERE id=?", (task["id"],)
            )
            self.ops_store.enqueue(task["kind"], task["id"], int(task["account_id"]))

    # ── HTTP 接口（只读状态 + 手动触发一轮）────────────────────────────────
    def install(self, app: FastAPI) -> None:
        router = APIRouter()

        @router.get("/api/orchestrator/status")
        def status():
            return {
                "ok": True,
                "data": {
                    "enabled": self.enabled,
                    "auto_publish": self.auto_publish_mode or False,
                    "auto_approve": self.auto_approve,
                    "mode": self.mode,
                    "asset_pool": str(self.asset_pool) if self.asset_pool else None,
                    "goals_loaded": len(self._load_goals()),
                    "daily_publish_limit": self.daily_publish_limit,
                    "tick_seconds": self.tick_seconds,
                    "running": bool(self._thread and self._thread.is_alive()),
                },
            }

        @router.post("/api/orchestrator/trigger")
        def trigger():
            if not self.enabled:
                return {"ok": False, "error": "orchestrator disabled (set XHS_ORCHESTRATOR=1)"}
            self._tick()
            return {"ok": True}

        app.include_router(router)
